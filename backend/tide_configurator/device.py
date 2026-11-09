"""SSH transport to the recorder's Unix socket; credentials stay in memory."""
import base64
import errno
import hashlib
import json
import os
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
import re
import shlex
import socket
import stat
import struct
import threading
import time
import uuid
from urllib.request import urlopen
import yaml

import paramiko
from .boards import load_board
from .vio import validate_package

ROOT = Path(__file__).resolve().parents[2]
CAMERA_SETTINGS_ERROR = '设备相机设置文件无效：需要 exposure_max_us=1..50000 和可选 fpn=0|1（FPN 校正）'


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

    def camera_settings(self):
        with self.lock:
            sftp = self.client.open_sftp()
            try:
                try:
                    with sftp.open(self.board.camera_settings, 'rb') as source:
                        data = source.read(4097)
                except OSError as exc:
                    if exc.errno != errno.ENOENT:
                        raise
                    return {'exposure_max_us': 500, 'fpn': True, 'source': 'default',
                            'path': self.board.camera_settings}
            finally:
                sftp.close()
            if len(data) > 4096:
                raise RuntimeError('设备相机设置文件过长')
            try:
                lines = [line.strip() for line in data.decode('ascii').splitlines()
                         if line.strip() and not line.lstrip().startswith('#')]
            except UnicodeDecodeError as exc:
                raise RuntimeError('设备相机设置文件不是 ASCII 文本') from exc
            values = {}
            for line in lines:
                key, separator, value = line.partition('=')
                key, value = key.strip(), value.strip()
                if not separator or key in values:
                    raise RuntimeError(CAMERA_SETTINGS_ERROR)
                if key == 'exposure_max_us':
                    if not re.fullmatch(r'[1-9][0-9]*', value) or int(value) > 50000:
                        raise RuntimeError(CAMERA_SETTINGS_ERROR)
                    values[key] = int(value)
                elif key == 'fpn':
                    if value not in ('0', '1'):
                        raise RuntimeError(CAMERA_SETTINGS_ERROR)
                    values[key] = value == '1'
                else:
                    raise RuntimeError(CAMERA_SETTINGS_ERROR)
            if 'exposure_max_us' not in values:
                raise RuntimeError(CAMERA_SETTINGS_ERROR)
            return {'exposure_max_us': values['exposure_max_us'],
                    'fpn': values.get('fpn', True), 'source': 'file',
                    'path': self.board.camera_settings}

    def camera_settings_save(self, exposure_max_us: int, fpn: bool):
        with self.lock:
            if type(exposure_max_us) is not int or not 1 <= exposure_max_us <= 50000:
                raise ValueError('自动曝光上限必须在 1..50000 µs 之间')
            if type(fpn) is not bool:
                raise ValueError('FPN 校正选项必须是布尔值')
            if self.remote:
                raise ValueError('请先关闭相机预览再修改相机设置')
            help_text = self.execute([self.recorder_path, '--help'])
            if 'TIDE_CAM_CONFIG_PATH' not in help_text or 'fpn' not in help_text:
                raise RuntimeError('设备录制器版本过旧，请先更新后再修改相机设置')
            destination = self.board.camera_settings
            parent = str(PurePosixPath(destination).parent)
            temporary = str(PurePosixPath(parent) / ('.camera.conf.' + uuid.uuid4().hex + '.tmp'))
            self.execute(['mkdir', '-p', parent])
            sftp = self.client.open_sftp()
            try:
                sftp.get_channel().settimeout(15)
                with sftp.open(temporary, 'wb') as target:
                    target.write((f'exposure_max_us={exposure_max_us}\n'
                                  f'fpn={int(fpn)}\n').encode('ascii'))
                    target.flush()
                self.execute(['mv', '-f', temporary, destination])
            except Exception:
                try:
                    sftp.remove(temporary)
                except OSError:
                    pass
                raise
            finally:
                sftp.close()
            return self.camera_settings()

    def execute(self, arguments, timeout=15):
        client = self.client
        if not client:
            raise ConnectionError('设备连接已中断，请重新连接')
        _, output, error = client.exec_command(shlex.join(arguments), timeout=timeout)
        data = output.read().decode(errors='replace')
        errors = error.read().decode(errors='replace')
        if output.channel.recv_exit_status():
            raise RuntimeError(errors.strip() or data.strip() or '设备命令失败')
        return data.strip()

    def _local_fpn_calib(self):
        override = os.environ.get('TIDE_CAM_FPN_CALIB')
        candidates = ([Path(override).expanduser()] if override else []) + [
            ROOT.parent / 'install/tide_cam/bin/tide_cam_fpn_calib',
            ROOT.parent / 'tide_cam/build/tide_cam_fpn_calib',
        ]
        for path in candidates:
            try:
                if path.is_file() and not path.is_symlink():
                    return path
            except OSError:
                continue
        return None

    def _remote_sha256(self, path):
        try:
            parts = self.execute(['sha256sum', path]).split()
        except RuntimeError:
            return None
        return parts[0] if parts and re.fullmatch(r'[0-9a-f]{64}', parts[0]) else None

    def _ensure_fpn_calib(self, progress):
        remote = self.board.fpn_calib
        local = self._local_fpn_calib()
        remote_hash = self._remote_sha256(remote)
        if local is None:
            if remote_hash:
                return remote
            raise RuntimeError(
                '设备缺少 FPN 标定工具，且电脑上找不到 tide_cam_fpn_calib；'
                '请先运行 tide_cam/build.sh 或设置 TIDE_CAM_FPN_CALIB')
        local_hash = hashlib.sha256(local.read_bytes()).hexdigest()
        if remote_hash == local_hash:
            return remote
        progress('tool_upload', '上传 FPN 标定工具', 0, 0, 0)
        parent = str(PurePosixPath(remote).parent)
        temporary = parent + '/.fpn_calib.' + uuid.uuid4().hex + '.tmp'
        self.execute(['mkdir', '-p', parent])
        sftp = self.client.open_sftp()
        sftp.get_channel().settimeout(60)
        try:
            try:
                sftp.put(str(local), temporary)
                sftp.chmod(temporary, 0o755)
            except Exception:
                try:
                    sftp.remove(temporary)
                except OSError:
                    pass
                raise
        finally:
            sftp.close()
        self.execute(['mv', '-f', temporary, remote])
        if self._remote_sha256(remote) != local_hash:
            raise RuntimeError('FPN 标定工具上传后校验失败')
        return remote

    def capture_fpn(self, mode, width, height, gains, frames, skip, exposure_us,
                    remote_root, local_root, progress):
        if mode not in ('dark', 'flat'):
            raise ValueError('无效的 FPN 采集模式')
        remote_root = PurePosixPath(str(remote_root))
        data_dir = PurePosixPath(self.board.data_dir)
        if (not remote_root.is_absolute() or '..' in remote_root.parts or
                data_dir not in remote_root.parents):
            raise ValueError('FPN 临时目录不在设备数据区')
        tool = self._ensure_fpn_calib(progress)
        remote_stage = str(remote_root / ('run-' + uuid.uuid4().hex))
        local_root = Path(local_root)
        local_root.mkdir(parents=True)
        self.execute(['mkdir', '-p', remote_stage])
        files = ('meta.txt', 'cam0_cols.f32', 'cam0_first.pgm', 'cam0_last.pgm',
                 'cam1_cols.f32', 'cam1_first.pgm', 'cam1_last.pgm')
        logs = []
        total = len(gains)
        try:
            for index, gain in enumerate(gains):
                progress('capture', f'采集增益 {gain}', index, total, gain)
                remote_out = remote_stage + f'/g{gain}'
                self.execute(['mkdir', '-p', remote_out])
                command = [tool, '--mode', mode, '--frames', str(frames),
                           '--skip', str(skip), '--exposure-us', str(exposure_us),
                           '--gain', str(gain), '--width', str(width),
                           '--height', str(height), '--out', remote_out]
                output = self.execute(command, timeout=180)
                logs.append(f'$ {shlex.join(command)}\n{output}\n')
                progress('download', f'回传增益 {gain}', index, total, gain)
                local_out = local_root / f'g{gain}'
                local_out.mkdir()
                sftp = self.client.open_sftp()
                sftp.get_channel().settimeout(60)
                try:
                    for name in files:
                        remote_path = remote_out + '/' + name
                        entry = sftp.lstat(remote_path)
                        if not stat.S_ISREG(entry.st_mode) or not 0 < entry.st_size <= 64 * 1024 * 1024:
                            raise RuntimeError(f'设备返回了无效的 {name}')
                        sftp.get(remote_path, str(local_out / name))
                finally:
                    sftp.close()
            (local_root / 'capture.log').write_text(''.join(logs))
        finally:
            if self.client:
                try:
                    sftp = self.client.open_sftp()
                    try:
                        self._remove_owned(sftp, remote_stage)
                    finally:
                        sftp.close()
                except Exception:
                    pass
        return {'gains': list(gains), 'width': width, 'height': height,
                'frames': frames, 'exposure_us': exposure_us}

    def apply_fpn_lut(self, local_dir):
        local_dir = Path(local_dir)
        remote_dir = PurePosixPath(self.board.fpn_lut_dir)
        self.execute(['mkdir', '-p', str(remote_dir)])
        uploads = []
        for cam in (0, 1):
            source = local_dir / f'cam{cam}.fpn'
            if not source.is_file() or source.is_symlink():
                raise ValueError(f'校正结果缺少 cam{cam}.fpn')
            data = source.read_bytes()
            if len(data) < 24 or len(data) > 64 * 1024 * 1024:
                raise ValueError(f'cam{cam}.fpn 文件长度无效')
            magic, version, header, width, height, count, flags, offset, _ = \
                struct.unpack_from('<4sHHHHHHII', data)
            if (magic != b'TFPN' or version != 2 or header != 24 or
                    offset != 24 or flags != 7 or not count or not width or not height or
                    len(data) != 24 + count * (8 + 4 * width)):
                raise ValueError(f'cam{cam}.fpn 不是新的暗场+平场格式')
            uploads.append((source, data))
        temporary = []
        try:
            sftp = self.client.open_sftp()
            sftp.get_channel().settimeout(60)
            try:
                for cam, (source, _data) in enumerate(uploads):
                    path = str(remote_dir / ('.cam%d.fpn.%s.tmp' % (cam, uuid.uuid4().hex)))
                    sftp.put(str(source), path)
                    sftp.chmod(path, 0o644)
                    temporary.append(path)
            finally:
                sftp.close()
            for cam, path in enumerate(temporary):
                self.execute(['mv', '-f', path, str(remote_dir / f'cam{cam}.fpn')])
        except Exception:
            if self.client:
                try:
                    sftp = self.client.open_sftp()
                    try:
                        for path in temporary:
                            try:
                                sftp.remove(path)
                            except OSError:
                                pass
                    finally:
                        sftp.close()
                except Exception:
                    pass
            raise
        return {
            'directory': str(remote_dir),
            'files': {f'cam{cam}.fpn': hashlib.sha256(data).hexdigest()
                      for cam, (_source, data) in enumerate(uploads)},
        }

    def _board_script(self, name, *arguments, timeout=30):
        if not self.client:
            raise RuntimeError('请先连接设备')
        if name not in {'vio-status.sh', 'vio-start.sh', 'vio-stop.sh',
                        'vio-deploy.sh', 'vio-apply.sh'}:
            raise ValueError('未知板级脚本')
        script = (ROOT / 'boards' / self.board.id / name).read_bytes()
        channel = self.client.get_transport().open_session(timeout=10)
        channel.settimeout(timeout)
        try:
            channel.exec_command('sh -s -- ' + shlex.join(str(arg) for arg in arguments))
            channel.sendall(script)
            channel.shutdown_write()
            output = channel.makefile('rb').read().decode(errors='replace').strip()
            error = channel.makefile_stderr('rb').read().decode(errors='replace').strip()
            if channel.recv_exit_status():
                raise RuntimeError(error[-2000:] or output[-2000:] or '板端脚本执行失败')
            return output
        finally:
            channel.close()

    def vio_status(self):
        with self.lock:
            output = self._board_script('vio-status.sh', self.board.vio)
            state, _, value = output.partition(' ')
            if state not in ('running', 'external', 'stopped'):
                raise RuntimeError('设备返回了无效的 VIO 状态')
            result = dict(state=state, pid=int(value) if value.isdigit() else None,
                          path=self.board.vio, config=self.board.vio_config,
                          log=self.board.vio_output + '/vio.log')
            try:
                parts = self.execute(['sha256sum', self.board.vio_config]).split()
                digest = parts[0] if parts else ''
                if not re.fullmatch(r'[0-9a-f]{64}', digest):
                    raise RuntimeError('设备返回的配置 SHA-256 无效')
                result['config_sha256'] = digest
            except RuntimeError:
                result['config_sha256'] = None
            if state == 'stopped':
                try:
                    lines = self.execute(['tail', '-n', '40', result['log']]).splitlines()
                    errors = [line.split('Error: ', 1)[1] for line in lines if 'Error: ' in line]
                    if errors:
                        result['last_error'] = errors[-1]
                except RuntimeError:
                    pass  # No VIO run has written a log yet.
            return result

    def ros_addresses(self):
        if not self.client:
            raise ConnectionError('请先连接设备')
        connection = self.client.get_transport().sock
        return (str(ip_address(connection.getsockname()[0])),
                str(ip_address(connection.getpeername()[0])))

    def vio_start(self):
        with self.lock:
            if self.remote:
                raise RuntimeError('启动 VIO 前请先关闭采集预览')
            if self.vio_status()['state'] != 'stopped':
                raise RuntimeError('VIO 已在运行')
            local_address, peer_address = self.ros_addresses()
            domain = int(os.environ.get('ROS_DOMAIN_ID', '0'))
            if not 0 <= domain <= 232:
                raise ValueError('ROS_DOMAIN_ID 必须在 0..232 之间')
            self._board_script('vio-start.sh', self.board.vio, self.board.vio_config,
                               self.board.vio_output, self.board.camera_settings,
                               peer_address, local_address, domain, timeout=20)
            status = self.vio_status()
            if status['state'] == 'stopped':
                raise RuntimeError('VIO 启动后立即退出；请查看设备日志')
            return status

    def vio_stop(self):
        with self.lock:
            self._board_script('vio-stop.sh', self.board.vio, timeout=25)
            return self.vio_status()

    def cpu_counters(self):
        # The heartbeat uses its own SSH channel, independently of long device
        # operations. Its caller closes the transport if the probe times out.
        first = self.execute(['cat', '/proc/stat']).splitlines()[0].split()
        if first[0] != 'cpu' or len(first) < 5:
            raise RuntimeError('设备返回了无效的 CPU 统计')
        ticks = [int(value) for value in first[1:]]
        # guest/guest_nice are already included in user/nice by Linux.
        return sum(ticks[:8]), ticks[3] + (ticks[4] if len(ticks) > 4 else 0)

    def vio_deploy(self, package: Path, progress):
        with self.lock:
            if self.vio_status()['state'] != 'stopped':
                raise RuntimeError('请先停止 VIO 再部署')
            format = validate_package(package, self.board)
            stage = self.board.data_dir.rstrip('/') + '/.tide-configurator-vio-' + uuid.uuid4().hex[:12]
            archive = stage + '/package.tar' + ('.gz' if format == 'gz' else '')
            sftp = self.client.open_sftp()
            sftp.get_channel().settimeout(60)
            try:
                sftp.mkdir(stage, mode=0o700)
                try:
                    sftp.put(str(package), archive, callback=lambda sent, total: progress('device_upload', sent, total))
                except Exception:
                    try:
                        sftp.remove(archive)
                        sftp.rmdir(stage)
                    except OSError:
                        pass
                    raise
            finally:
                sftp.close()
            progress('install', 0, 0)
            try:
                output = self._board_script('vio-deploy.sh', archive,
                                            str(PurePosixPath(self.board.vio).parent), stage,
                                            format, timeout=120)
                lines = output.splitlines()
                return {'path': str(PurePosixPath(self.board.vio).parent),
                        'backup': next((line.removeprefix('backup ') for line in lines
                                        if line.startswith('backup ')), '')}
            except Exception:
                # The board script removes its own stage. Clean up if SSH failed before it ran.
                try:
                    sftp = self.client.open_sftp()
                    try:
                        sftp.remove(archive)
                        sftp.rmdir(stage)
                    finally:
                        sftp.close()
                except OSError:
                    pass
                raise

    def vio_apply(self, config: Path):
        with self.lock:
            if self.vio_status()['state'] != 'stopped':
                raise RuntimeError('请先停止 VIO 再应用标定配置')
            checksum = hashlib.sha256(config.read_bytes()).hexdigest()
            destination = PurePosixPath(self.board.vio_config)
            temporary = str(destination.parent / ('.' + destination.name + '.' + uuid.uuid4().hex + '.tmp'))
            sftp = self.client.open_sftp()
            sftp.get_channel().settimeout(60)
            try:
                sftp.put(str(config), temporary)
            except Exception:
                try:
                    sftp.remove(temporary)
                except OSError:
                    pass
                raise
            finally:
                sftp.close()
            try:
                output = self._board_script('vio-apply.sh', str(destination), temporary,
                                            checksum, timeout=30)
            except Exception:
                try:
                    sftp = self.client.open_sftp()
                    try:
                        sftp.remove(temporary)
                    finally:
                        sftp.close()
                except OSError:
                    pass
                raise
            backup = next((line.removeprefix('backup ') for line in output.splitlines()
                           if line.startswith('backup ')), '')
            return {'path': str(destination), 'backup': backup, 'sha256': checksum}

    def close_session(self):
        with self.lock:
            self._close_session()

    def connect(self, host, port, username, password, board_id='tidesense'):
        with self.lock:
            if self.client:
                raise RuntimeError('请先断开当前设备')
            board = load_board(board_id)
            client = paramiko.SSHClient()
            known_hosts = Path.home() / '.ssh' / 'known_hosts'
            known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
                # Dropbear's non-login PATH omits /sbin, where SDK devmem lives.
                command = shlex.join(['env', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
                                      'TIDE_CAM_CONFIG_PATH=' + self.board.camera_settings] + arguments)
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
        client = self.client
        report = progress
        def progress(done, total):
            if self.client is not client:
                raise ConnectionError('设备连接已中断，请重新连接')
            report(done, total)
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
            if authorized and self.client is client:
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
            transport = self.client.get_transport() if self.client else None
            if self.client and (transport is None or not transport.is_active()):
                self.drop_connection()
                return
            self._close_session()
            if self.client:
                self.client.close()
            self.client = self.daemon = self.remote = None
            self.info = {'connected': False}

    def drop_connection(self):
        """Discard a lost connection locally; never wait for remote cleanup."""
        client, daemon, link = self.client, self.daemon, self.link
        self.client = self.daemon = self.remote = self.link = None
        self.preview_url = self.http_base = self.http_token = None
        self.info = {'connected': False}
        if client:
            client.close()
        if daemon:
            daemon.close()
        if link:
            link[0].close()
