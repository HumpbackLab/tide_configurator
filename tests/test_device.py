from unittest.mock import MagicMock
from io import BytesIO
import errno
import socket
import threading

import paramiko
import pytest

from tide_configurator.device import Device


@pytest.mark.parametrize('cached,hashed,port', [
    (None, False, 22),
    ('matching', False, 22),
    ('matching', True, 2222),
    ('changed', False, 22),
])
def test_connect_uses_user_known_hosts(tmp_path, monkeypatch, cached, hashed, port):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    known_hosts = tmp_path / 'home' / '.ssh' / 'known_hosts'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    host = 'device.example'
    lookup = host if port == 22 else f'[{host}]:{port}'
    key = paramiko.ECDSAKey.generate()
    old_key = paramiko.ECDSAKey.generate()
    # A stale private cache must not override the shared user cache.
    (workspace / 'known_hosts').write_text(f'{lookup} {old_key.get_name()} {old_key.get_base64()}\n')
    before = None
    if cached:
        known_hosts.parent.mkdir(parents=True)
        entries = paramiko.HostKeys()
        entries.add('other.example', old_key.get_name(), old_key)
        entries.add(paramiko.HostKeys.hash_host(lookup) if hashed else lookup,
                    key.get_name(), key if cached == 'matching' else old_key)
        entries.save(str(known_hosts))
        before = known_hosts.read_bytes()

    class PasswordServer(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL

    client_socket, server_socket = socket.socketpair()
    server = paramiko.Transport(server_socket)
    server.add_server_key(key)
    server.start_server(event=threading.Event(), server=PasswordServer())
    connect = paramiko.SSHClient.connect

    def connect_over_socket(client, *args, **kwargs):
        return connect(client, *args, sock=client_socket, **kwargs)

    monkeypatch.setattr(paramiko.SSHClient, 'connect', connect_over_socket)
    device = Device(workspace)
    device.execute = MagicMock(side_effect=[
        'test-kernel', 'aarch64', '/dev/root 100 10 90 10% /mnt/data',
    ])
    try:
        if cached == 'changed':
            with pytest.raises(paramiko.BadHostKeyException):
                device.connect(host, port, 'root', 'test-password')
            assert device.client is None
            assert known_hosts.read_bytes() == before
        else:
            assert device.connect(host, port, 'root', 'test-password')['connected']
            saved = paramiko.HostKeys(str(known_hosts))
            assert saved.check(lookup, key)
            if cached:
                assert saved.check('other.example', old_key)
            else:
                assert known_hosts.parent.stat().st_mode & 0o777 == 0o700
        assert old_key.get_base64() in (workspace / 'known_hosts').read_text()
    finally:
        if device.client:
            device.client.close()
        server.close()
        client_socket.close()
        server_socket.close()


def test_camera_settings_use_sftp_without_device_helper(tmp_path):
    device = Device(tmp_path)
    device.client = MagicMock()
    files = {}
    updated_recorder = False

    class RemoteUpload(BytesIO):
        def __init__(self, path):
            super().__init__()
            self.path = path

        def close(self):
            if not self.closed:
                files[self.path] = self.getvalue()
            super().close()

    def remote_open(path, mode='r'):
        if mode == 'wb':
            return RemoteUpload(path)
        if path not in files:
            raise FileNotFoundError(errno.ENOENT, 'No such file', path)
        return BytesIO(files[path])

    sftp = device.client.open_sftp.return_value
    sftp.open.side_effect = remote_open

    def execute(args):
        if args == [device.recorder_path, '--help']:
            return 'TIDE_CAM_CONFIG_PATH' if updated_recorder else 'old recorder'
        if args[:2] == ['mkdir', '-p']:
            return ''
        if args[:2] == ['mv', '-f']:
            files[args[3]] = files.pop(args[2])
            return ''
        raise AssertionError(args)

    device.execute = execute
    assert device.camera_settings()['source'] == 'default'
    with pytest.raises(RuntimeError, match='版本过旧'):
        device.camera_settings_save(1200)
    updated_recorder = True
    assert device.camera_settings_save(1200)['exposure_max_us'] == 1200
    assert files[device.board.camera_settings] == b'exposure_max_us=1200\n'
    files[device.board.camera_settings] = b'# edited on device\nexposure_max_us = 700\n'
    assert device.camera_settings()['exposure_max_us'] == 700
    files[device.board.camera_settings] = b'exposure_max_us=0\n'
    with pytest.raises(RuntimeError, match='设置文件无效'):
        device.camera_settings()
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
