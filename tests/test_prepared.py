import hashlib
from pathlib import Path

import numpy as np
import pytest
import yaml
import zstandard

from tide_calibration import prepared, recording


@pytest.fixture
def source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'COMPLETE').touch()
    (source / 'metadata.yaml').write_text(yaml.safe_dump(dict(schema='tidesense-dataset-v2', complete=True, pairs=3, imu_samples=41)))
    residual = np.zeros((800, 640), np.uint8)
    residual[:, 0] = 123
    with (source / 'frames.bin').open('wb') as f:
        f.write(b'TIDESENSE_ZST_V1\n')
        for i in range(3):
            t = 10**9+i*50_000_000
            header = recording.HEADER.pack(0x54444341, 1, 640, 400, i+1, t, t+2_000_000, i, i, 1, 0)
            data = zstandard.ZstdCompressor(write_checksum=True).compress(header+residual.tobytes())
            f.write(len(data).to_bytes(4, 'little')+data)
    (source / 'imu.csv').write_text(''.join(f'{950_000_000+i*5_000_000},0,0,0,0,0,9.8\n' for i in range(41)))
    return source


def test_one_pass_hashes_and_reuses_bag_without_reading_raw_again(source, tmp_path, monkeypatch):
    original_frames = recording.frames
    calls = []
    def frames(*args, **kwargs):
        calls.append(args[0])
        yield from original_frames(*args, **kwargs)
    monkeypatch.setattr(recording, 'frames', frames)
    events = []
    cache = tmp_path / 'cache'
    result = prepared.prepare(source, cache, events.append)
    assert len(calls) == 1 and result['report']['pairs'] == 3
    assert result['input_sha256'] == {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in prepared.NAMES}
    assert events[-1]['frames'] == 3 and not events[-1]['cached']
    bag = tmp_path / 'result.bag'
    prepared.materialize(result, bag)
    assert bag.stat().st_ino == (cache/'dataset.bag').stat().st_ino
    second = prepared.prepare(source, cache)
    assert second['cached'] and len(calls) == 1
    assert second['input_sha256'] == result['input_sha256']


def test_changed_input_invalidates_cache_and_corruption_is_rejected(source, tmp_path):
    cache = tmp_path/'cache'
    prepared.prepare(source, cache)
    path = source/'frames.bin'
    data = bytearray(path.read_bytes()); data[-1] ^= 1; path.write_bytes(data)
    with pytest.raises(ValueError, match='checksum'):
        prepared.prepare(source, cache)


def test_failed_preparation_never_publishes_cache(source, tmp_path):
    cache = tmp_path/'cache'
    (source/'imu.csv').write_text('1,0,0,0,0,0,9.8\n')
    with pytest.raises(ValueError):
        prepared.prepare(source, cache)
    assert not (cache/'prepared.json').exists()
    assert not (cache/'dataset.bag').exists()


def test_change_during_preparation_is_rejected(source, tmp_path):
    changed = False
    def progress(event):
        nonlocal changed
        if event.get('frames') and not changed:
            path = source/'metadata.yaml'
            path.write_bytes(path.read_bytes()+b'\n')
            changed = True
    with pytest.raises(ValueError, match='changed during preparation'):
        prepared.prepare(source, tmp_path/'cache', progress)
    assert not (tmp_path/'cache/prepared.json').exists()


def test_changed_cached_bag_is_regenerated(source, tmp_path):
    cache = tmp_path/'cache'
    result = prepared.prepare(source, cache)
    with (cache/'dataset.bag').open('r+b') as f:
        f.write(b'broken')
    second = prepared.prepare(source, cache)
    assert not second['cached']
    assert second['input_sha256'] == result['input_sha256']
    assert (cache/'dataset.bag').read_bytes().startswith(b'#ROSBAG')


def test_change_after_preparation_prevents_materializing_stale_bag(source, tmp_path):
    result = prepared.prepare(source, tmp_path/'cache')
    path = source/'metadata.yaml'
    path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError, match='changed after preparation'):
        prepared.materialize(result, tmp_path/'stale.bag')
    assert not (tmp_path/'stale.bag').exists()
