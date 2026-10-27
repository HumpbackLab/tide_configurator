import io
import struct
import tarfile
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import pytest

from tide_configurator.app import create_app
from tide_configurator.boards import load_board
from tide_configurator.device import Device
from tide_configurator.vio import validate_package


ROOT = 'schurvins-gcc11-static-micro-ros'


def test_stopped_vio_surfaces_the_last_program_error(tmp_path):
    device = Device(tmp_path)
    device._board_script = lambda *args: 'stopped'
    device.execute = lambda args: 'startup\nError: IMU sample timing gap reaches 15ms limit\n'
    assert device.vio_status()['last_error'] == 'IMU sample timing gap reaches 15ms limit'


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as tar:
        for name, data in entries.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
    return output.getvalue()


def release():
    elf = bytearray(64)
    elf[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', elf, 18, 183)
    return {f'{ROOT}/run.sh': b'#!/bin/sh\n',
            f'{ROOT}/bin/schurvins_stereo': bytes(elf),
            f'{ROOT}/schurvins.yaml': b'cameras: []\n',
            f'{ROOT}/SHA256SUMS': b''}


def test_package_must_match_board_and_reject_unsafe_entries(tmp_path):
    path = tmp_path / 'release.tar'
    path.write_bytes(archive(release()))
    assert validate_package(path, load_board('tidesense')) == 'tar'
    for name in ('wrong-board/run.sh', f'{ROOT}/../escape', '/absolute', f'{ROOT}/./duplicate'):
        bad = release()
        bad[name] = b'x'
        path.write_bytes(archive(bad))
        with pytest.raises(ValueError, match='不安全|不属于'):
            validate_package(path, load_board('tidesense'))
    bad = release()
    bad[f'{ROOT}/bin/schurvins_stereo'] = b'host program'
    path.write_bytes(archive(bad))
    with pytest.raises(ValueError, match='AArch64'):
        validate_package(path, load_board('tidesense'))


def test_upload_and_vio_controls_are_separate_from_recording(tmp_path):
    with TestClient(create_app(tmp_path / 'workspace')) as client:
        state = client.app.state.console
        state.device.info = {'connected': True, 'host': 'device'}
        state.device.vio_status = lambda: {'state': 'stopped'}
        calls = []
        def deploy(path, progress):
            calls.append(path.read_bytes())
            progress('device_upload', path.stat().st_size, path.stat().st_size)
            return {'path': '/mnt/data/schurvins-gcc11-static-micro-ros', 'backup': '/mnt/data/backup'}
        state.device.vio_deploy = deploy
        body = archive(release())
        response = client.post('/api/vio/deploy', content=body,
                               headers={'Content-Type': 'application/octet-stream'})
        assert response.status_code == 200, response.text
        assert calls == [body]
        assert state.vio_transfer['status'] == 'complete'
        assert not list(state.workspace.glob('.vio-upload-*'))
        assert client.post('/api/vio/deploy', content=b'bad').status_code == 400
        assert not list(state.workspace.glob('.vio-upload-*'))
        state.device.close_session = MagicMock()
        state.device.vio_start = lambda: {'state': 'running', 'pid': 42}
        state.device.vio_stop = lambda: {'state': 'stopped'}
        state.recorder = {'state': 'preview'}
        assert client.post('/api/vio/start').json()['pid'] == 42
        state.device.close_session.assert_called_once()
        assert state.recorder is None
        assert client.post('/api/vio/stop').json()['state'] == 'stopped'
