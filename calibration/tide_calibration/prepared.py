"""Single-pass validation/hash/bag preparation with identity-checked reuse."""
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from . import recording

NAMES = ('frames.bin', 'imu.csv', 'metadata.yaml')


def identity(path, *, bag=False):
    st = path.stat()
    result = dict(device=st.st_dev, inode=st.st_ino, size=st.st_size, mtime_ns=st.st_mtime_ns)
    # A hard link changes ctime, but doesn't change our read-only bag contents.
    if not bag:
        result['ctime_ns'] = st.st_ctime_ns
    return result


def producer():
    digest = hashlib.sha256()
    for path in (Path(__file__), Path(recording.__file__)):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def cache_for(source):
    root = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()
    return root / 'tidesense-calibration' / key


@contextmanager
def lock(cache):
    cache.parent.mkdir(parents=True, exist_ok=True)
    with (cache.parent / (cache.name + '.lock')).open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def save(cache, manifest):
    path = cache / 'prepared.tmp'
    path.write_text(json.dumps(manifest, indent=2, allow_nan=False))
    path.replace(cache / 'prepared.json')


def prepare(source, cache=None, progress=None):
    source = Path(source).resolve(strict=True)
    cache = Path(cache or cache_for(source)).resolve()
    if source == cache or source in cache.parents or cache in source.parents:
        raise ValueError('Prepared cache must be outside the input recording')
    def emit(**fields):
        if progress:
            progress(fields)
    emit(stage='prepare', frames=0, pairs=0, bytes=0, total=0, cached=False)
    with lock(cache):
        meta, metadata_bytes = recording.metadata(source)
        inputs = {name: identity(source / name) for name in NAMES}
        code = producer()
        total = inputs['frames.bin']['size']
        try:
            manifest = json.loads((cache / 'prepared.json').read_text())
            valid = (manifest['source'] == str(source) and manifest['inputs'] == inputs
                     and manifest['producer'] == code
                     and manifest['report']['pairs'] == meta['pairs']
                     and manifest['report']['imu_samples'] == meta['imu_samples']
                     and set(manifest['input_sha256']) == set(NAMES)
                     and all(isinstance(h, str) and len(h)==64 and all(c in '0123456789abcdef' for c in h)
                             for h in manifest['input_sha256'].values())
                     and manifest['bag'] == identity(cache / 'dataset.bag', bag=True))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            valid = False
        if valid:
            emit(stage='prepare', frames=manifest['report']['pairs'], pairs=manifest['report']['pairs'],
                 bytes=total, total=total, cached=True)
            return dict(manifest, directory=str(cache), cached=True)
        hashes = {name: hashlib.sha256() for name in NAMES}
        hashes['metadata.yaml'].update(metadata_bytes)
        last = 0
        def update(processed, count):
            nonlocal last
            now = time.monotonic()
            if now-last >= .1 or count == meta.get('pairs'):
                emit(stage='prepare', frames=count, pairs=meta['pairs'], bytes=processed, total=total, cached=False)
                last = now
        with tempfile.TemporaryDirectory(prefix=cache.name + '-', dir=cache.parent) as temporary:
            bag = Path(temporary) / 'dataset.bag'
            report = recording.write_bag(source, bag, validator=recording.Validator(meta), hashes=hashes, progress=update)
            if inputs != {name: identity(source / name) for name in NAMES}:
                raise ValueError('Recording changed during preparation; discard derived results')
            recording.metadata(source)  # Completion markers must still be valid.
            cache.mkdir(exist_ok=True)
            bag.replace(cache / 'dataset.bag')
            manifest = dict(source=str(source), inputs=inputs, producer=code, report=report,
                            input_sha256={name: digest.hexdigest() for name, digest in hashes.items()},
                            bag=identity(cache / 'dataset.bag', bag=True))
            save(cache, manifest)
        emit(stage='prepare', frames=report['pairs'], pairs=report['pairs'], bytes=total, total=total, cached=False)
        return dict(manifest, directory=str(cache), cached=False)


def materialize(prepared, destination, progress=None):
    """Kalibr opens its input bag read-only; same-filesystem runs share its inode."""
    cache = Path(prepared['directory'])
    destination = Path(destination)
    with lock(cache):
        raw = Path(prepared['source'])
        recording.metadata(raw)
        if prepared['inputs'] != {name: identity(raw / name) for name in NAMES}:
            raise ValueError('Recording changed after preparation; prepare again')
        source = cache / 'dataset.bag'
        if identity(source, bag=True) != prepared['bag']:
            raise ValueError('Prepared bag changed; prepare again')
        try:
            os.link(source, destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            with source.open('rb') as src, destination.open('xb') as dst:
                done = 0
                while block := src.read(4*1024*1024):
                    dst.write(block)
                    done += len(block)
                    if progress:
                        progress(dict(stage='bag', bytes=done, total=prepared['bag']['size']))
        if progress:
            progress(dict(stage='bag', bytes=prepared['bag']['size'], total=prepared['bag']['size']))
