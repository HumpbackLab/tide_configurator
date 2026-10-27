"""Explicitly recover good stereo packets into a new, provenance-marked copy."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct

import yaml
import zstandard

from . import recording
from .prepared import identity, NAMES


def recover(source, destination, progress=None):
    source, destination = Path(source).resolve(strict=True), Path(destination).resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Recovered copy must be outside the original recording')
    meta = yaml.safe_load((source/'metadata.yaml').read_bytes())
    if (not isinstance(meta, dict) or not meta.get('complete')
            or meta.get('schema') not in ('tidesense-dataset-v1', 'tidesense-dataset-v2') or meta.get('recovery')):
        raise ValueError('Recovery requires an original finished recording')
    before = {name: identity(source/name) for name in NAMES}
    destination.mkdir()
    (destination/'INCOMPLETE').touch()
    dropped, gaps = [], {}
    count = good = 0
    first = last = previous = None
    maximum_gap = 0
    digest = hashlib.sha256()
    decoder = zstandard.ZstdDecompressor()
    try:
        with (source/'frames.bin').open('rb') as src, (destination/'frames.bin').open('wb') as dst:
            reader = recording._HashReader(src, digest, None)
            magic = reader.read(len(recording.MAGIC))
            if magic != b'TIDESENSE_ZST_V1\n':
                raise ValueError('Only independently checksummed compressed packets can be recovered')
            dst.write(magic)
            while True:
                offset = reader.tell()
                header = reader.read(4)
                if not header:
                    break
                if len(header) != 4:
                    raise ValueError('Truncated record length cannot be recovered')
                length = struct.unpack('<I', header)[0]
                if not 0 < length <= recording.MAX_RECORD+recording.MAX_RECORD//128+4096:
                    raise ValueError('Invalid record boundary cannot be recovered')
                packet = reader.read(length)
                if len(packet) != length:
                    raise ValueError('Truncated packet cannot be recovered')
                count += 1
                try:
                    content_size = zstandard.frame_content_size(packet)
                    if content_size not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR) and content_size > recording.MAX_RECORD:
                        raise zstandard.ZstdError('Oversized decoded frame')
                    raw = decoder.decompress(packet, max_output_size=recording.MAX_RECORD)
                except zstandard.ZstdError as exc:
                    dropped.append(dict(frame=count, byte_offset=offset, reason=str(exc)))
                    gaps[good] = gaps.get(good, 0)+1
                else:
                    if len(raw) < recording.HEADER.size:
                        raise ValueError('Invalid decoded header; refusing uncertain recovery')
                    h = recording.HEADER.unpack_from(raw)
                    if len(raw) != recording.HEADER.size+2*recording.image_bytes(h):
                        raise ValueError('Invalid decoded dimensions; refusing uncertain recovery')
                    dst.write(header); dst.write(packet)
                    if first is None:
                        first = h[5]
                    if previous is not None:
                        maximum_gap = max(maximum_gap, h[5]-previous)
                    previous = last = h[5]
                    good += 1
                if progress and count % 10 == 0:
                    progress(dict(stage='recover', bytes=reader.tell(), total=before['frames.bin']['size'],
                                  frames=count, pairs=meta['pairs'], dropped=len(dropped)))
            dst.flush(); os.fsync(dst.fileno())
        if count != meta['pairs'] or good < 2:
            raise ValueError('Source packet counts do not match metadata or too few good frames remain')
        if not dropped:
            raise ValueError('No corrupt camera packets found; the error cannot be repaired by dropping images')
        for name in ('imu.csv', 'camera-config.yaml', 'imu-source.yaml'):
            if (source/name).exists():
                shutil.copyfile(source/name, destination/name)
        if before != {name: identity(source/name) for name in NAMES}:
            raise ValueError('Original recording changed during recovery')
        # Timing and encoded pixels are untouched. Only explicitly declared
        # camera holes are allowed; IMU continuity remains strict.
        for key in tuple(meta):
            if any(key.startswith(prefix) for prefix in ('compression_', 'image_write_', 'writer_', 'raw_queue_',
                                                       'encoded_queue_', 'finalize_', 'data_sync_', 'expected_frames_')):
                meta.pop(key)
        meta.update(pairs=good, encoded_pairs=good, compressed_image_bytes=(destination/'frames.bin').stat().st_size-17, first_camera_ns=first, last_camera_ns=last, max_camera_gap_ms=maximum_gap/1e6,
                    camera_hz_measured=(good-1)*1e9/(last-first), archive_verified=False,
                    recovery=dict(source=str(source), source_frames_sha256=digest.hexdigest(), source_pairs=count,
                                  dropped_camera_frames=dropped,
                                  camera_gaps=[dict(after_pair=k, count=v) for k,v in sorted(gaps.items())]))
        (destination/'metadata.yaml').write_text(yaml.safe_dump(meta, sort_keys=False))
        (destination/'INCOMPLETE').rename(destination/'COMPLETE')
        report = recording.validate(destination)
        meta['archive_verified'] = True
        (destination/'metadata.yaml').write_text(yaml.safe_dump(meta, sort_keys=False))
        (destination/'recovery.json').write_text(json.dumps(meta['recovery'], indent=2))
        return report
    except BaseException:
        if (destination/'COMPLETE').exists():
            (destination/'COMPLETE').rename(destination/'INCOMPLETE')
        raise
