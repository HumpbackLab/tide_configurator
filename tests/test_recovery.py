import hashlib
import struct

import pytest
import yaml

from test_prepared import source
from tide_calibration import recording, recovery, prepared


def damage(source, index):
    path=source/'frames.bin';data=bytearray(path.read_bytes());offset=len(recording.MAGIC)
    for _ in range(index):
        size=struct.unpack_from('<I',data,offset)[0];offset+=4+size
    size=struct.unpack_from('<I',data,offset)[0];data[offset+4+size-1]^=1;path.write_bytes(data)


def test_recovery_preserves_pixels_times_imu_and_original_bytes(source, tmp_path):
    good=list(recording.frames(source));damage(source,1)
    original={p.name:p.read_bytes() for p in source.iterdir()}
    destination=tmp_path/'recovered'
    report=recovery.recover(source,destination)
    assert report['pairs']==2 and report['dropped_camera_pairs']==1
    restored=list(recording.frames(destination))
    assert restored==[good[0],good[2]]
    assert (destination/'imu.csv').read_bytes()==(source/'imu.csv').read_bytes()
    meta=yaml.safe_load((destination/'metadata.yaml').read_bytes())
    assert meta['recovery']['dropped_camera_frames'][0]['frame']==2
    assert meta['recovery']['source_frames_sha256']==hashlib.sha256(original['frames.bin']).hexdigest()
    assert original=={p.name:p.read_bytes() for p in source.iterdir()}
    assert prepared.prepare(destination,tmp_path/'cache')['report']['dropped_camera_pairs']==1
    # Removing the explicit provenance must restore strict camera checks.
    meta.pop('recovery');(destination/'metadata.yaml').write_text(yaml.safe_dump(meta))
    with pytest.raises(ValueError,match='Camera gap'):
        recording.validate(destination)


def test_recovery_does_not_hide_imu_gaps_or_truncation(source,tmp_path):
    damage(source,1)
    path=source/'imu.csv';lines=path.read_text().splitlines();path.write_text('\n'.join(lines[:10]+lines[11:])+'\n')
    with pytest.raises(ValueError,match='IMU gap'):
        recovery.recover(source,tmp_path/'recovered')
    assert not (tmp_path/'recovered/COMPLETE').exists()


def test_truncated_packet_cannot_be_skipped(source,tmp_path):
    path=source/'frames.bin';path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError,match='Truncated'):
        recovery.recover(source,tmp_path/'recovered')
    assert not (tmp_path/'recovered/COMPLETE').exists()
