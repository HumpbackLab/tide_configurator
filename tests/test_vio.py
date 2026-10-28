import io
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tarfile
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

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


def test_vio_status_reports_device_configuration_hash(tmp_path):
    device = Device(tmp_path)
    digest = 'ab' * 32
    device._board_script = lambda *args: 'stopped'
    device.execute = lambda args: f'{digest}  {device.board.vio_config}'
    assert device.vio_status()['config_sha256'] == digest
    device.execute = lambda args: (_ for _ in ()).throw(RuntimeError('No such file'))
    assert device.vio_status()['config_sha256'] is None


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
        state.ros.start = AsyncMock()
        state.ros.stop = AsyncMock()
        state.recorder = {'state': 'preview'}
        assert client.post('/api/vio/start').json()['pid'] == 42
        state.device.close_session.assert_called_once()
        assert state.recorder is None
        assert client.post('/api/vio/stop').json()['state'] == 'stopped'


def pose(stamp, x, frame='odom'):
    return SimpleNamespace(
        header=SimpleNamespace(frame_id=frame, stamp=SimpleNamespace(sec=stamp, nanosec=0)),
        pose=SimpleNamespace(
            position=SimpleNamespace(x=x, y=0.0, z=0.0),
            orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0)))


def test_ros_trajectory_websocket_tracks_segments_and_resets(tmp_path):
    with TestClient(create_app(tmp_path / 'workspace')) as client:
        state = client.app.state.console
        state.on_pose(pose(1, 0.0))
        state.on_pose(pose(2, 1.0))
        with client.websocket_connect('/api/vio/trajectory') as socket:
            initial = socket.receive_json()
            assert initial['reset'] and len(initial['points']) == 2
            assert client.get('/api/vio/trajectory/download').status_code == 200
            state.on_tracking(False)
            state.on_pose(pose(3, 2.0))
            update = socket.receive_json()
            assert update['points'][0]['segment'] == 1
            first_file = state.trajectory_path
            state.on_pose(pose(1, 0.0))  # Timestamp went backwards: new VIO session.
            for _ in range(3):
                reset = socket.receive_json()
                if reset['reset']:
                    break
            assert reset['reset'] and len(reset['points']) == 1
            assert len(first_file.read_text().splitlines()) == 3


def test_trajectory_keeps_and_streams_more_than_3000_ros_poses(tmp_path):
    with TestClient(create_app(tmp_path / 'workspace')) as client:
        state = client.app.state.console
        for stamp in range(3205):
            state.on_pose(pose(stamp, float(stamp)))
        assert state.pose_seq == 3205
        assert len(state.trajectory_path.read_text().splitlines()) == 3205
        response = client.get('/api/vio/trajectory/download')
        assert response.status_code == 200
        assert len(response.content.splitlines()) == 3205
        with client.websocket_connect('/api/vio/trajectory') as socket:
            received = []
            while len(received) < 3205:
                packet = socket.receive_json()
                received.extend(packet['points'])
            assert [received[0]['seq'], received[-1]['seq']] == [1, 3205]


def test_device_cpu_counters_are_read_from_proc_stat(tmp_path):
    device = Device(tmp_path)
    device.client = MagicMock()
    device.execute = lambda args: 'cpu  10 0 20 60 10 0 0 0 0 0\ncpu0 10 0 20 60 10\n'
    assert device.cpu_counters() == (100, 70)


def test_vio_start_passes_the_computer_side_ipv4_to_board(tmp_path):
    device = Device(tmp_path)
    device.client = MagicMock()
    device.client.get_transport().sock.getsockname.return_value = ('192.168.123.2', 43210)
    device.vio_status = MagicMock(side_effect=[{'state': 'stopped'}, {'state': 'running'}])
    device._board_script = MagicMock()
    assert device.vio_start()['state'] == 'running'
    call = device._board_script.call_args
    assert call.args[-1] == '192.168.123.2'
    assert call.args[0] == 'vio-start.sh'


def test_apply_requires_completed_calibration_and_stopped_vio(tmp_path):
    identifier = 'a' * 32
    folder = tmp_path / 'workspace/jobs' / identifier
    result = folder / 'result'
    result.mkdir(parents=True)
    (folder / 'job.json').write_text(json.dumps({'id': identifier, 'status': 'complete',
                                                  'created': '2026-01-01'}))
    config = result / 'schurvins.yaml'
    config.write_text('cameras: [{}, {}]\nimu_params: {}\n')
    (result / 'COMPLETE').touch()
    with TestClient(create_app(tmp_path / 'workspace')) as client:
        state = client.app.state.console
        endpoint = f'/api/calibrations/{identifier}/apply'
        assert client.post(endpoint).status_code == 400
        state.device.info = {'connected': True}
        state.device.vio_status = lambda: {'state': 'running'}
        assert client.post(endpoint).status_code == 400
        current = {'sha256': '0' * 64}
        digest = hashlib.sha256(config.read_bytes()).hexdigest()
        state.device.vio_status = lambda: {'state': 'stopped', 'config_sha256': current['sha256']}
        def apply(path):
            current['sha256'] = digest
            return {'path': '/mnt/data/vio/schurvins.yaml', 'backup': '/mnt/data/vio/old',
                    'sha256': digest}
        state.device.vio_apply = MagicMock(side_effect=apply)
        response = client.post(endpoint)
        assert response.status_code == 200, response.text
        assert response.json()['job_id'] == identifier
        state.device.vio_apply.assert_called_once_with(config)
        snapshot = client.get('/api/state').json()
        assert snapshot['jobs'][0]['config_sha256'] == snapshot['vio']['config_sha256'] == digest
        (result / 'COMPLETE').unlink()
        assert client.post(endpoint).status_code == 400
        assert state.device.vio_apply.call_count == 1


def test_apply_script_backs_up_and_updates_manifest(tmp_path):
    script = Path(__file__).parents[1] / 'boards/tidesense/vio-apply.sh'
    target = tmp_path / 'schurvins.yaml'
    staged = tmp_path / '.schurvins.yaml.upload.tmp'
    other = tmp_path / 'bin'
    target.write_text('old calibration\n')
    other.write_text('program')
    manifest = tmp_path / 'SHA256SUMS'
    def checksum(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    manifest.write_text(f'{checksum(target)}  schurvins.yaml\n{checksum(other)}  bin\n')
    staged.write_text('new calibration\n')
    bad = subprocess.run(['sh', str(script), str(target), str(staged), '0' * 64],
                         capture_output=True, text=True)
    assert bad.returncode != 0
    assert target.read_text() == 'old calibration\n'
    applied = subprocess.run(['sh', str(script), str(target), str(staged), checksum(staged)],
                             capture_output=True, text=True)
    assert applied.returncode == 0, applied.stderr
    assert target.read_text() == 'new calibration\n'
    backup = Path(next(line.removeprefix('backup ') for line in applied.stdout.splitlines()
                       if line.startswith('backup ')))
    assert backup.read_text() == 'old calibration\n'
    assert not staged.exists()
    verified = subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=tmp_path,
                              capture_output=True, text=True)
    assert verified.returncode == 0, verified.stdout + verified.stderr
