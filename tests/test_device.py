from unittest.mock import MagicMock
import threading

import pytest

from tide_configurator.device import Device


def test_camera_settings_use_device_tool_and_reject_old_recorder(tmp_path):
    device = Device(tmp_path)
    device.client = MagicMock()
    stored = {'value': 500, 'updated_recorder': False}
    def execute(args):
        if args == [device.recorder_path, '--help']:
            return 'TIDE_CAM_CONFIG_PATH' if stored['updated_recorder'] else 'old recorder'
        if args[:2] == ['test', '-x']:
            return ''
        if args[0] == device.board.camera_settings_tool:
            if args[1] == 'set':
                stored['value'] = int(args[3])
            return f"exposure_max_us={stored['value']}\nsource=file"
        raise AssertionError(args)
    device.execute = execute
    assert device.camera_settings()['exposure_max_us'] == 500
    with pytest.raises(RuntimeError, match='版本过旧'):
        device.camera_settings_save(1200)
    stored['updated_recorder'] = True
    assert device.camera_settings_save(1200)['exposure_max_us'] == 1200
    device.remote = '/tmp/active-preview'
    with pytest.raises(ValueError, match='关闭相机预览'):
        device.camera_settings_save(1500)


def test_failed_camera_start_is_cleaned_up_and_can_retry(tmp_path):
    device = Device(tmp_path)
    device.client = MagicMock()
    device.info = {'connected': True, 'host': '192.168.123.99'}
    daemon = device.client.get_transport.return_value.open_session.return_value
    daemon.exit_status_ready.return_value = True
    sftp = device.client.open_sftp.return_value
    log = b'Session error: Camera is in use or /run is not writable\n'
    sftp.open.return_value.__enter__.return_value.read.return_value = log
    sftp.stat.return_value.st_size = len(log)
    sftp.listdir_attr.return_value = []
    device.execute = lambda args: '/dev/iio:device0' if args[0] == 'sh' else log.decode()
    device.request = MagicMock(side_effect=RuntimeError('connection closed'))
    with pytest.raises(RuntimeError, match='相机正被其他采集程序占用'):
        device.preview()
    assert device.remote is None and device.daemon is None
    assert (tmp_path / 'device-last.log').read_bytes() == log
    sftp.rmdir.assert_called_once()
    sftp.put.assert_not_called()
    device.client.close.assert_not_called()
    # The failure must not poison the connected device or prevent a fresh start.
    device.request = MagicMock(return_value={'state': 'preview', 'preview_port': 8123})
    assert device.preview() == {'state': 'preview', 'preview_port': 8123}
    assert device.preview_url.startswith('http://192.168.123.99:8123/')
    assert device.preview_url.endswith('/stream.mjpg')
    assert device.remote is not None


def test_missing_imu_releases_session_directory(tmp_path):
    device = Device(tmp_path)
    device.client = MagicMock()
    sftp = device.client.open_sftp.return_value
    sftp.listdir_attr.return_value = []
    sftp.stat.side_effect = FileNotFoundError
    device.execute = lambda args: 'unexpected-device'
    with pytest.raises(RuntimeError, match='没有找到 SC7U22'):
        device.preview()
    assert device.remote is None and device.daemon is None
    sftp.rmdir.assert_called_once()
    sftp.put.assert_not_called()


def test_missing_device_recorder_does_not_create_session(tmp_path):
    device = Device(tmp_path)
    device.client = MagicMock()
    device.execute = MagicMock(side_effect=RuntimeError('missing'))
    with pytest.raises(RuntimeError, match='设备上没有可执行录制器'):
        device.preview()
    device.client.open_sftp.assert_not_called()
    assert device.remote is None


def test_native_recording_failure_is_not_reported_as_a_transport_error(tmp_path):
    device = Device(tmp_path)
    device.client = MagicMock()
    device.remote = '/tmp/session'
    log = ('INCOMPLETE pairs=176\nSession error: Recording queue full: '
           'compression/write throughput cannot sustain capture; dataset INCOMPLETE')
    device.execute = lambda args: log
    reason, failed = device.failure('录制器连接已关闭')
    assert failed and '写入队列已满' in reason and '数据不完整' in reason
    assert (tmp_path / 'device-last.log').read_text() == log + '\n'


def test_missing_native_failure_keeps_transport_error(tmp_path):
    device = Device(tmp_path)
    assert device.failure('network lost') == ('network lost', False)


def test_waits_for_final_log_after_control_socket_closes(tmp_path):
    device = Device(tmp_path)
    device.client = MagicMock()
    device.remote = '/tmp/session'
    device.daemon = MagicMock()
    exited = threading.Event()
    device.daemon.exit_status_ready.side_effect = exited.is_set
    device.execute = lambda args: 'Session error: Recording queue full' if exited.is_set() else ''
    timer = threading.Timer(.02, exited.set)
    timer.start()
    try:
        reason, failed = device.failure('录制器连接已关闭')
        assert failed and '写入队列已满' in reason
    finally:
        timer.join()


def test_control_channel_transfers_json_only_and_reuses_one_relay(tmp_path):
    device = Device(tmp_path)
    device.client = MagicMock()
    device.remote = '/tmp/session'
    channel = device.client.get_transport.return_value.open_session.return_value
    channel.closed = False
    reader = channel.makefile.return_value
    reader.readline.return_value = b'{"ok":true,"state":"preview","preview_port":8080}\n'
    assert device.request({'command': 'status'})['preview_port'] == 8080
    assert device.request({'command': 'stop'})['state'] == 'preview'
    channel.exec_command.assert_called_once_with(device.recorder_path + ' --relay /tmp/session/control.sock')
    reader.read.assert_not_called()
    assert channel.sendall.call_count == 2


def test_http_preview_address_is_session_scoped_and_cleared_on_release(tmp_path):
    device = Device(tmp_path)
    device.info = {'connected': True, 'host': '192.168.123.99'}
    device.http_token = 'a'*32
    device._preview_info({'preview_port': 12345})
    assert device.preview_url == 'http://192.168.123.99:12345/' + 'a'*32 + '/stream.mjpg'
    device._release_session()
    assert device.preview_url is None and device.http_token is None
    with pytest.raises(RuntimeError, match='HTTP'):
        device._preview_info({'preview_port': 0})
