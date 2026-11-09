import asyncio
import json
import stat
import struct
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tide_configurator import camera_calibration
from tide_configurator.app import State
from tide_configurator.device import Device


WIDTH = 64
HEIGHT = 8
FRAMES = 8
BLACK = 12.0
OFFSET = np.tile(np.array([-4.0, 4.0, -2.0, 2.0]), WIDTH // 4)
FLAT_PATTERN = np.tile(np.array([-1.0, 1.0, -0.5, 0.5]), WIDTH // 4)


def write_capture(root, mode, gain, *, width=WIDTH, height=HEIGHT, frames=FRAMES,
                  saturation=0.0):
    directory = Path(root) / f'g{gain}'
    directory.mkdir(parents=True)
    offset = np.resize(OFFSET, width)
    pattern = np.resize(FLAT_PATTERN, width)
    meta = [
        f'mode={mode}', f'width={width}', f'height={height}', f'frames={frames}',
        'skip=0', 'exposure_us=500', f'gain_q10={gain}',
    ]
    for cam in (0, 1):
        meta.append(f'cam{cam}_saturated_fraction={saturation}')
        if mode == 'dark':
            frame = BLACK + offset
        else:
            frame = BLACK + offset + 120.0 * (1.0 + 0.15 * pattern)
        columns = np.tile(frame, (frames, 1)).astype(np.float32)
        columns.tofile(directory / f'cam{cam}_cols.f32')
    (directory / 'meta.txt').write_text('\n'.join(meta) + '\n')
    return directory


def write_pair(root):
    root = Path(root)
    for gain in (1024, 2048):
        write_capture(root / 'dark', 'dark', gain)
        write_capture(root / 'flat', 'flat', gain)
    return root


def read_header(data):
    return struct.unpack_from('<4sHHHHHHII', data)


def test_compute_generates_version_two_black_dark_and_flat_lut(tmp_path):
    write_pair(tmp_path)
    result = camera_calibration.compute(
        tmp_path / 'dark', tmp_path / 'flat', tmp_path / 'result')

    assert result['version'] == 2
    assert result['flags'] == 7
    assert result['width'] == WIDTH and result['height'] == HEIGHT
    assert result['gains'] == [1024, 2048]
    assert set(result['files']) == {'cam0.fpn', 'cam1.fpn'}

    for cam in (0, 1):
        data = (tmp_path / 'result' / f'cam{cam}.fpn').read_bytes()
        magic, version, header, width, height, count, flags, offset, reserved = \
            read_header(data)
        assert magic == b'TFPN'
        assert (version, header, width, height, count, flags) == \
            (2, 24, WIDTH, HEIGHT, 2, 7)
        assert offset == 24 and reserved == 0
        assert len(data) == 24 + count * (8 + 4 * width)
        gains = [struct.unpack_from('<I', data, 24 + index *
                                    (8 + 4 * width))[0] for index in range(count)]
        assert gains == [1024, 2048]

    for camera in result['cameras']:
        for bucket in camera['buckets']:
            assert bucket['black_lsb'] == pytest.approx(BLACK)
            assert bucket['dark_reduction'] > 90
            assert bucket['flat_reduction'] > 80
            assert bucket['flat_saturation'] == 0

    saved = json.loads((tmp_path / 'result' / 'result.json').read_text())
    assert saved == result


def test_validate_capture_checks_gains_dimensions_and_file_size(tmp_path):
    dark = tmp_path / 'dark'
    write_capture(dark, 'dark', 1024)
    write_capture(dark, 'dark', 2048)
    summary = camera_calibration.validate_capture(dark, 'dark', [1024, 2048])
    assert summary['gains'] == [1024, 2048]
    assert summary['width'] == WIDTH and summary['frames'] == FRAMES

    with pytest.raises(ValueError, match='请求'):
        camera_calibration.validate_capture(dark, 'dark', [1024, 4096])
    (dark / 'g2048' / 'cam1_cols.f32').write_bytes(b'')
    with pytest.raises(ValueError, match='长度'):
        camera_calibration.validate_capture(dark, 'dark', [1024, 2048])


def test_compute_rejects_missing_mismatched_gains_and_saturated_flat(tmp_path):
    with pytest.raises(ValueError, match='尚未采集'):
        camera_calibration.compute(
            tmp_path / 'dark', tmp_path / 'flat', tmp_path / 'result')

    write_capture(tmp_path / 'dark', 'dark', 1024)
    write_capture(tmp_path / 'dark', 'dark', 2048)
    write_capture(tmp_path / 'flat', 'flat', 1024)
    write_capture(tmp_path / 'flat', 'flat', 4096)
    with pytest.raises(ValueError, match='增益集合不一致'):
        camera_calibration.compute(
            tmp_path / 'dark', tmp_path / 'flat', tmp_path / 'result')

    flat = tmp_path / 'flat'
    for path in flat.iterdir():
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
    write_capture(flat, 'flat', 1024, saturation=0.02)
    write_capture(flat, 'flat', 2048, saturation=0.02)
    with pytest.raises(ValueError, match='过曝'):
        camera_calibration.compute(
            tmp_path / 'dark', flat, tmp_path / 'result')


def test_calibration_state_is_persisted_and_interrupted_after_restart(tmp_path):
    state = State(tmp_path)
    state.camera_calibration.update(status='running', stage='capture_dark')
    state._save_camera_calibration()

    restarted = State(tmp_path)
    assert restarted.camera_calibration['status'] == 'failed'
    assert '重新采集' in restarted.camera_calibration['error']


def test_state_capture_compute_and_apply_flow(tmp_path):
    async def run():
        state = State(tmp_path)
        state.device.info = {'connected': True, 'host': 'device'}
        state.device.vio_status = lambda: {'state': 'stopped'}
        calls = []

        def capture(mode, width, height, gains, frames, skip, exposure_us,
                    remote_root, local_root, progress):
            calls.append((mode, width, height, tuple(gains), frames, skip,
                          exposure_us, remote_root))
            for gain in gains:
                write_capture(local_root, mode, gain, width=width, height=height,
                              frames=frames)
            progress('download', 'done', len(gains), len(gains), gains[-1])

        applied = []

        def apply_fpn_lut(directory):
            applied.append(Path(directory))
            return {'directory': '/mnt/data/ov9281-fpn', 'files': {}}

        state.device.capture_fpn = capture
        state.device.apply_fpn_lut = apply_fpn_lut
        for mode in ('dark', 'flat'):
            body = SimpleNamespace(
                mode=mode, width=640, height=400, gains=[2048, 1024],
                frames=8, skip=0, exposure_us=500)
            await state.start_camera_capture(body)
            task = state.camera_task
            await task
            assert state.camera_calibration[mode]['gains'] == [1024, 2048]
        assert calls == [
            ('dark', 640, 400, (1024, 2048), 8, 0, 500,
             '/mnt/data/tide-configurator/camera-calibration'),
            ('flat', 640, 400, (1024, 2048), 8, 0, 500,
             '/mnt/data/tide-configurator/camera-calibration'),
        ]

        result = await state.compute_camera_calibration()
        assert result['gains'] == [1024, 2048]
        assert (tmp_path / 'camera-calibration' / 'result' / 'cam0.fpn').is_file()
        deployment = await state.apply_camera_calibration()
        assert deployment['directory'] == '/mnt/data/ov9281-fpn'
        assert applied == [tmp_path / 'camera-calibration' / 'result']
        assert state.camera_calibration['applied']['directory'] == \
            '/mnt/data/ov9281-fpn'

    asyncio.run(run())


class FakeStat:
    st_mode = stat.S_IFREG | 0o644
    st_size = 16


class FakeSFTP:
    def __init__(self):
        self.uploaded = {}
        self.downloaded = []
        self.modes = {}

    def get_channel(self):
        return SimpleNamespace(settimeout=lambda _timeout: None)

    def lstat(self, path):
        return FakeStat()

    def get(self, remote, local):
        self.downloaded.append(remote)
        Path(local).write_bytes(b'data')

    def put(self, local, remote):
        self.uploaded[remote] = Path(local).read_bytes()

    def chmod(self, path, mode):
        self.modes[path] = mode

    def remove(self, path):
        raise FileNotFoundError(path)

    def close(self):
        pass


def test_device_capture_downloads_each_gain_and_cleans_remote(tmp_path, monkeypatch):
    device = Device(tmp_path)
    device.client = SimpleNamespace(open_sftp=FakeSFTP)
    commands = []
    device.execute = lambda arguments, timeout=15: commands.append((arguments, timeout)) or ''
    monkeypatch.setattr(device, '_ensure_fpn_calib', lambda progress: '/mnt/tool/fpn_calib')
    progress = []
    summary = device.capture_fpn(
        'dark', 640, 400, [1024, 2048], 8, 2, 500,
        '/mnt/data/tide-configurator/camera-calibration', tmp_path / 'local',
        lambda *arguments: progress.append(arguments))

    assert summary == {
        'gains': [1024, 2048], 'width': 640, 'height': 400,
        'frames': 8, 'exposure_us': 500}
    tool_commands = [arguments for arguments, _timeout in commands
                     if arguments[0] == '/mnt/tool/fpn_calib']
    assert len(tool_commands) == 2
    assert all(mode == 0o755 for path, mode in device.client.open_sftp().modes.items())
    assert tool_commands[0][tool_commands[0].index('--gain') + 1] == '1024'
    assert tool_commands[1][tool_commands[1].index('--gain') + 1] == '2048'
    assert all(timeout == 180 for arguments, timeout in commands
               if arguments[0] == '/mnt/tool/fpn_calib')
    for gain in (1024, 2048):
        directory = tmp_path / 'local' / f'g{gain}'
        assert sorted(path.name for path in directory.iterdir()) == [
            'cam0_cols.f32', 'cam0_first.pgm', 'cam0_last.pgm',
            'cam1_cols.f32', 'cam1_first.pgm', 'cam1_last.pgm', 'meta.txt']
    assert (tmp_path / 'local' / 'capture.log').is_file()
    assert progress[0][0] == 'capture'


def test_device_rejects_capture_paths_outside_data_dir(tmp_path):
    device = Device(tmp_path)
    with pytest.raises(ValueError, match='数据区'):
        device.capture_fpn(
            'dark', 640, 400, [1024, 2048], 8, 0, 500,
            '/tmp/camera-calibration', tmp_path / 'local', lambda *args: None)


def test_apply_fpn_lut_validates_format_and_uploads_atomically(tmp_path):
    write_pair(tmp_path)
    result = camera_calibration.compute(
        tmp_path / 'dark', tmp_path / 'flat', tmp_path / 'result')
    device = Device(tmp_path)
    sftp = FakeSFTP()
    device.client = SimpleNamespace(open_sftp=lambda: sftp)
    commands = []
    device.execute = lambda arguments, timeout=15: commands.append(arguments) or ''

    applied = device.apply_fpn_lut(tmp_path / 'result')
    assert applied['directory'] == '/mnt/data/ov9281-fpn'
    assert applied['files'] == result['files']
    assert len(sftp.uploaded) == 2
    assert all(path.startswith('/mnt/data/ov9281-fpn/.cam') for path in sftp.uploaded)
    assert set(sftp.modes.values()) == {0o644}
    assert ['mkdir', '-p', '/mnt/data/ov9281-fpn'] in commands
    assert any(command[:2] == ['mv', '-f'] for command in commands)

    data = bytearray((tmp_path / 'result' / 'cam0.fpn').read_bytes())
    struct.pack_into('<H', data, 14, 3)
    (tmp_path / 'result' / 'cam0.fpn').write_bytes(data)
    with pytest.raises(ValueError, match='暗场\\+平场'):
        device.apply_fpn_lut(tmp_path / 'result')
