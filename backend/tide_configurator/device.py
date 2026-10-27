"""SSH transport to the recorder's Unix socket; credentials stay in memory."""
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import socket
import stat
import threading
import time
import uuid
from urllib.request import urlopen
import yaml

import paramiko
from .boards import load_board

ROOT = Path(__file__).resolve().parents[2]


class Device:
    def __init__(self, workspace: Path, board_id: str = 'tidesense'):
        self.workspace = workspace
        self.client = None
        self.daemon = None
        self.link = None
        self.preview_url = None
        self.http_base = None
        self.http_token = None
        self.lock = threading.RLock()
        self.control_lock = threading.RLock()
        self.info = {'connected': False}
        self.remote = None
        self.board = load_board(board_id)

    @property
    def recorder_path(self):
        return self.board.recorder

    def execute(self, arguments):
        _, output, error = self.client.exec_command(shlex.join(arguments), timeout=15)
        data = output.read().decode(errors='replace')
        errors = error.read().decode(errors='replace')
        if output.channel.recv_exit_status():
            raise RuntimeError(errors.strip() or data.strip() or '设备命令失败')
        return data.strip()

    def connect(self, host, port, username, password, board_id='tidesense'):
        with self.lock:
            if self.client:
                raise RuntimeError('请先断开当前设备')
            board = load_board(board_id)
            client = paramiko.SSHClient()
            known_hosts = self.workspace / 'known_hosts'
            if known_hosts.exists():
                client.load_host_keys(str(known_hosts))
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(host, port=port, username=username, password=password or None,
                               timeout=8, banner_timeout=8, auth_timeout=8,
                               look_for_keys=not bool(password), allow_agent=not bool(password))
                client.save_host_keys(str(known_hosts))
                self.client = client
                kernel = self.execute(['uname', '-r'])
                architecture = self.execute(['uname', '-m'])
                if architecture != board.architecture:
                    raise RuntimeError(f'板型 {board.id} 需要 {board.architecture}，设备报告 {architecture}')
                disk = self.execute(['df', '-Pk', board.data_dir]).splitlines()[-1].split()
                key = client.get_transport().get_remote_server_key()
                self.board = board
                self.info = dict(connected=True, host=host, port=port, username=username,
                                 board=board.id, model=board.model, architecture=architecture, kernel=kernel,
                                 free_bytes=int(disk[3]) * 1024,
                                 fingerprint='SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('='))
                return self.info
            except Exception:
                client.close()
                self.client = None
                raise

    def _relay(self):
        if self.link and not self.link[0].closed:
            return self.link
        channel = self.client.get_transport().open_session(timeout=5)
        channel.settimeout(5)
        channel.exec_command(shlex.join([self.recorder_path, '--relay', self.remote + '/control.sock']))
        self.link = channel, channel.makefile('rb')
        return self.link

    def request(self, command):
        with self.control_lock:
            if not self.client or not self.remote:
                raise RuntimeError('请先启动设备预览')
            channel, reader = self._relay()
            try:
                channel.sendall((json.dumps(command) + '\n').encode())
                line = reader.readline(200000)
                if not line or not line.endswith(b'\n'):
                    raise RuntimeError('录制器连接已关闭')
                message = json.loads(line)
                if not message.get('ok'):
                    raise RuntimeError(message.get('error', '录制器拒绝命令'))
                return message
            except (OSError, ValueError, EOFError, RuntimeError):
                channel.close()
                self.link = None
                raise

    def _preview_info(self, message):
        port = message.get('preview_port')
        if type(port) is not int or not 0 < port < 65536 or not self.http_token:
            raise RuntimeError('录制器没有提供有效的 HTTP 预览地址')
        host = self.info['host']
        if ':' in host:
            host = '[' + host + ']'
        self.http_base = f'http://{host}:{port}/{self.http_token}'
        self.preview_url = None if message.get('state') == 'files' else self.http_base + '/stream.mjpg'
        return message

    def preview(self, width=640, height=400):
        with self.lock:
            if self.remote:
                try:
                    message = self.request({'command': 'status'})
                    if message['state'] != 'files':
                        return self._preview_info(message)
                    self._close_session()
                except (OSError, RuntimeError):
                    if self.daemon and not self.daemon.exit_status_ready():
                        raise
                    self._release_session()
            return self._launch(width, height)

    def _ensure_http(self):
        with self.lock:
            if self.remote:
                try:
                    self.request({'command': 'status'})
                    return
                except (OSError, RuntimeError):
                    if self.daemon and not self.daemon.exit_status_ready():
                        raise
                    self._release_session()
            self._launch(0, 0, files_only=True)

    def _launch(self, width, height, files_only=False):
        with self.lock:
            if not self.client:
                raise RuntimeError('请先连接设备')
            try:
                self.execute(['test', '-x', self.recorder_path])
            except RuntimeError as exc:
                raise RuntimeError(f'设备上没有可执行录制器：{self.recorder_path}') from exc
            remote = '/tmp/tide-configurator-' + uuid.uuid4().hex[:10]
            sftp = self.client.open_sftp()
            sftp.get_channel().settimeout(15)
            created = False
            try:
                sftp.mkdir(remote, mode=0o700)
                created = True
            except Exception:
                if created:
                    try:
                        self._remove_owned(sftp, remote)
                    except Exception:
                        pass
                raise
            finally:
                sftp.close()
            self.remote = remote
            self.http_token = uuid.uuid4().hex
            try:
                arguments = [self.recorder_path, '--file-server' if files_only else '--session',
                             '--socket', remote + '/control.sock', '--http-token', self.http_token]
                if not files_only:
                    script = ('for n in /sys/bus/iio/devices/iio:device*; do '
                              '[ "$(cat "$n/name" 2>/dev/null)" = ' + shlex.quote(self.board.imu_name) + ' ] '
                              '&& printf "/dev/%s\\n" "${n##*/}" && exit 0; done; exit 1')
                    imu = self.execute(['sh', '-c', script])
                    if not imu.startswith('/dev/iio:device') or '\n' in imu:
                        raise RuntimeError(f'没有找到 {self.board.imu_name.upper()} IMU')
                    arguments += ['--imu', imu, '--width', str(width), '--height', str(height)]
                command = shlex.join(arguments)
                self.daemon = self.client.get_transport().open_session()
                self.daemon.exec_command('exec ' + command + ' > ' + shlex.quote(remote + '/session.log') + ' 2>&1')
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    try:
                        message = self.request({'command': 'status'})
                        if message['state'] == ('files' if files_only else 'preview'):
                            return self._preview_info(message)
                    except (OSError, RuntimeError, ValueError):
                        if self.link:
                            self.link[0].close()
                            self.link = None
                    if self.daemon.exit_status_ready():
                        break
                    time.sleep(.25)
                error = self.execute(['tail', '-n', '12', remote + '/session.log'])
                if 'Camera is in use' in error:
                    raise RuntimeError('相机正被其他采集程序占用，请先停止该程序后重试。')
                raise RuntimeError('采集启动失败：' + error[-2000:])
            except Exception:
                if self.daemon and not self.daemon.exit_status_ready():
                    try:
                        self.request({'command': 'close'})
                    except Exception:
                        pass
                    deadline = time.monotonic() + 8
                    while not self.daemon.exit_status_ready() and time.monotonic() < deadline:
                        time.sleep(.05)
                if not self.daemon or self.daemon.exit_status_ready():
                    self._release_session()
                raise

    def _release_session(self):
        """Release an exited session while retaining the SSH device connection."""
        if self.link:
            self.link[0].close()
        self.link = None
        self.preview_url = self.http_base = self.http_token = None
        if self.daemon:
            self.daemon.close()
        if self.client and self.remote:
            try:
                sftp = self.client.open_sftp()
                sftp.get_channel().settimeout(5)
                try:
                    try:
                        path = self.remote + '/session.log'
                        length = sftp.stat(path).st_size
                        with sftp.open(path) as log:
                            log.seek(max(0, length-65536))
                            (self.workspace / 'device-last.log').write_bytes(log.read())
                    except OSError:
                        pass
                    self._remove_owned(sftp, self.remote)
                finally:
                    sftp.close()
            except Exception:
                # A disconnected transport can leave /tmp files; sensors are already closed.
                pass
        self.daemon = self.remote = None

    def _remove_owned(self, sftp, path):
        # Only our marked calibration workspace or freshly allocated session directory.
        for entry in sftp.listdir_attr(path):
            child = path + '/' + entry.filename
            if stat.S_ISDIR(entry.st_mode):
                self._remove_owned(sftp, child)
            else:
                sftp.remove(child)
        sftp.rmdir(path)

    def delete_recording(self, path, recording_id):
        """Remove one identified recording, never a shared workspace or another capture."""
        remote = PurePosixPath(path)
        data_dir = PurePosixPath(self.board.data_dir)
        if (not remote.is_absolute() or '..' in remote.parts or
                remote in (data_dir, data_dir / 'tide-configurator') or
                data_dir not in remote.parents):
            raise ValueError('录制目录不在可删除的设备数据区')
        with self.lock:
            if not self.client or not self.info.get('connected'):
                raise ValueError('请先连接录制所在的设备再删除')
            sftp = self.client.open_sftp()
            try:
                try:
                    folder = sftp.lstat(str(remote))
                except OSError as exc:
                    if getattr(exc, 'errno', None) == 2:
                        return
                    raise
                if not stat.S_ISDIR(folder.st_mode):
                    raise ValueError('设备上的录制目录不是普通目录')
                with sftp.open(str(remote / 'camera-config.yaml'), 'r') as source:
                    config = yaml.safe_load(source.read())
                if not isinstance(config, dict) or config.get('recording_id') != recording_id:
                    raise ValueError('设备录制编号不匹配，拒绝删除')
                allowed = {'COMPLETE', 'INCOMPLETE', 'camera-config.yaml', 'frames.bin',
                           'imu-source.yaml', 'imu.csv', 'metadata.yaml'}
                entries = sftp.listdir_attr(str(remote))
                if any(entry.filename not in allowed or not stat.S_ISREG(entry.st_mode)
                       for entry in entries):
                    raise ValueError('录制目录包含未知文件，拒绝删除')
                for entry in entries:
                    sftp.remove(str(remote / entry.filename))
                sftp.rmdir(str(remote))
            finally:
                sftp.close()

    def failure(self, fallback):
        """Recover the native failure after its control socket has closed."""
        if not self.client or not self.remote:
            return fallback, False
        try:
            # The preview socket can close during teardown, before the main
            # thread has written its final error to session.log.
            deadline = time.monotonic() + 1.5
            while self.daemon and not self.daemon.exit_status_ready() and time.monotonic() < deadline:
                time.sleep(.05)
            log = self.execute(['tail', '-c', '8192', self.remote + '/session.log'])
            (self.workspace / 'device-last.log').write_text(log + '\n')
            for line in reversed(log.splitlines()):
                if line.startswith('Session error: '):
                    reason = line.removeprefix('Session error: ')
                    if 'Recording queue full' in reason:
                        reason = '录制失败：图像压缩或写盘速度跟不上采集，写入队列已满；数据不完整，请重新录制。'
                    else:
                        reason = '录制器退出：' + reason
                    return reason, True
        except Exception:
            pass
        return fallback, False

    def start(self, kind, output, seconds):
        with self.lock:
            current = self.request({'command': 'status'})
            if current['state'] != 'preview':
                raise RuntimeError('录制器正在录制或保存，请等待完成')
            root = self.board.data_dir.rstrip('/') + '/tide-configurator'
            sftp = self.client.open_sftp()
            try:
                try:
                    root_entry = sftp.lstat(root)
                    if not stat.S_ISDIR(root_entry.st_mode):
                        raise RuntimeError('设备工作目录必须为真实目录，不能是符号链接')
                except IOError:
                    sftp.mkdir(root)
                    with sftp.open(root + '/.owner', 'w') as marker:
                        marker.write('TideConfigurator\n')
                with sftp.open(root + '/.owner') as marker:
                    if marker.read() != b'TideConfigurator\n':
                        raise RuntimeError('设备工作目录不属于 TideConfigurator')
                if kind == 'calibration':
                    output = root + '/calibration'
                    try:
                        entry = sftp.lstat(output)
                    except IOError:
                        entry = None
                    if entry:
                        if not stat.S_ISDIR(entry.st_mode):
                            raise RuntimeError('标定工作路径不是目录')
                        self._remove_owned(sftp, output)
                elif not output:
                    output = root + '/dataset-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
                else:
                    path = PurePosixPath(output)
                    if not path.is_absolute() or '..' in path.parts or str(path) in ('/', self.board.data_dir, root):
                        raise RuntimeError('请指定一个新的数据集目录')
                    output = str(path)
                    try:
                        sftp.lstat(output)
                    except IOError:
                        pass
                    else:
                        raise RuntimeError('数据集目录已存在，请指定新路径')
                response = self.request({'command': 'start', 'output': output, 'seconds': seconds, 'recording_id': uuid.uuid4().hex})
                return output, response
            finally:
                sftp.close()

    def retrieve(self, remote, local, progress, expected_id=None):
        self._ensure_http()
        token = uuid.uuid4().hex
        authorized = False
        try:
            try:
                manifest = self.request({'command': 'download', 'output': remote,
                                         'recording_id': expected_id or '', 'token': token})
            except RuntimeError as exc:
                if 'has been replaced' in str(exc):
                    raise RuntimeError('设备上的临时录制已被替换，无法取回所选录制') from exc
                raise
            authorized = True
            names = {'frames.bin', 'imu.csv', 'metadata.yaml', 'camera-config.yaml', 'imu-source.yaml'}
            files = manifest.get('files', [])
            if (len(files) != len(names) or {f.get('name') for f in files} != names
                    or any(type(f.get('size')) is not int or f['size'] <= 0 for f in files)):
                raise RuntimeError('设备返回了无效的录制文件清单')
            total = sum(f['size'] for f in files)
            local.mkdir()
            (local / 'INCOMPLETE').touch()
            done = 0
            progress(done, total)
            for entry in files:
                url = self.http_base + '/download/' + token + '/' + entry['name']
                received = 0
                with urlopen(url, timeout=20) as response, (local / entry['name']).open('wb') as output:
                    if int(response.headers.get('Content-Length', -1)) != entry['size']:
                        raise RuntimeError('下载文件长度与授权清单不一致')
                    while block := response.read(512*1024):
                        output.write(block)
                        received += len(block)
                        progress(done + received, total)
                if received != entry['size']:
                    raise RuntimeError('HTTP 下载中断，录制文件不完整')
                done += received
            config = yaml.safe_load((local / 'camera-config.yaml').read_bytes())
            if config.get('recording_id') != expected_id:
                raise RuntimeError('设备上的临时录制已被替换，无法取回所选录制')
            (local / 'INCOMPLETE').rename(local / 'COMPLETE')
        finally:
            if authorized:
                try:
                    self.request({'command': 'download_release', 'token': token})
                except (OSError, RuntimeError):
                    pass

    def _close_session(self):
        if not self.remote:
            return
        try:
            response = self.request({'command': 'status'})
        except (OSError, EOFError, RuntimeError, socket.timeout):
            response = None
        if response and response['state'] in ('recording', 'preparing', 'finalizing'):
            raise RuntimeError('请先停止录制并等待保存完成')
        try:
            self.request({'command': 'close'})
        except (OSError, EOFError, RuntimeError):
            pass
        if self.daemon:
            deadline = time.monotonic() + 8
            while not self.daemon.exit_status_ready() and time.monotonic() < deadline:
                time.sleep(.05)
            if not self.daemon.exit_status_ready():
                raise RuntimeError('采集进程尚未退出，请稍后重试断开连接')
        self._release_session()

    def disconnect(self):
        with self.lock:
            self._close_session()
            if self.client:
                self.client.close()
            self.client = self.daemon = self.remote = None
            self.info = {'connected': False}
