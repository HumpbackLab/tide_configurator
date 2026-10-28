"""TideConfigurator: local HTTP API, shared preview and calibration jobs."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid
import time
from urllib.parse import urlparse
import yaml

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from tide_calibration import recording, prepared as preparation, recovery

from .device import Device, ROOT
from .boards import list_boards
from .vio import MAX_ARCHIVE_BYTES, validate_package
from . import target
from .jobs import Jobs, write_json, tail
from .ros import RosTelemetry


class Connect(BaseModel):
    board: str = Field(default='tidesense', pattern=r'^[a-z][a-z0-9_]*$')
    host: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9_.:\-]+$')
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default='root', min_length=1, max_length=64)
    password: SecretStr = SecretStr('')


class Preview(BaseModel):
    width: int = 640
    height: int = 400


class CameraSettings(BaseModel):
    exposure_max_us: int = Field(ge=1, le=50000)


class Start(BaseModel):
    kind: str = Field(pattern='^(calibration|dataset)$')
    output: str = Field(default='', max_length=4096)
    seconds: int = Field(default=120, ge=0, le=86400)
    board_width_mm: float | None = Field(default=None, gt=0, le=10000, allow_inf_nan=False)


class ImportCapture(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class Calibrate(BaseModel):
    capture_id: str
    lens: str = Field(default='wide', pattern='^(wide|fisheye)$')
    noise: str = Field(min_length=1, max_length=20000)
    board_width_mm: float = Field(gt=0, le=10000, allow_inf_nan=False)


class State:
    def __init__(self, workspace):
        self.workspace = workspace
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'captures').mkdir(exist_ok=True)
        self.device = Device(workspace)
        self.vio = {'state': 'disconnected'}
        self.vio_transfer = {'status': 'idle', 'stage': '', 'bytes': 0, 'total': 0}
        self.cpu_percent = None
        self.cpu_poller = None
        self.ros_error = ''
        self.trajectory_dir = workspace / 'vio-trajectories'
        self.trajectory_dir.mkdir(exist_ok=True)
        self.pose_session = uuid.uuid4().hex
        self.trajectory_path = self.trajectory_dir / (self.pose_session + '.jsonl')
        self.trajectory_file = None
        self.pose_seq = 0
        self.pose_segment = 0
        self.last_pose_stamp = None
        self.last_pose_frame = None
        self.tracking = False
        self.pose_revision = 0
        self.ros = RosTelemetry(workspace, self.on_pose, self.on_tracking)
        self.jobs = Jobs(workspace)
        self.recorder = None
        self.frame = None
        self.poller = None
        self.active = None
        self.error = ''
        self.capture_error = ''
        self.transfers = {}
        self.transfer_tasks = set()
        self.operation = asyncio.Lock()
        self.captures = json.loads((workspace / 'captures.json').read_text()) if (workspace / 'captures.json').exists() else []
        for item in self.captures:
            if item['status'] in ('preparing', 'recording', 'finalizing'):
                item.update(status='interrupted', error='服务已重启，请检查设备上的原始录制完成标记。')
        for item in self.captures:
            if item.get('preparation_error') and not item.get('local'):
                self.transfers[item['id']] = dict(status='failed', stage='prepare', bytes=0, total=0, error=item['preparation_error'])
        self.save()

    def save(self):
        write_json(self.workspace / 'captures.json', self.captures)

    def capture(self, identifier):
        for item in self.captures:
            if item['id'] == identifier:
                return item
        raise ValueError('录制不存在')

    def _remove_capture_files(self, item):
        """Only delete directories created inside this application's workspace."""
        identifier = item['id']
        for field, subdir, name in (
            ('local', 'captures', None),
            ('failed_download', 'captures', None),
            ('local', 'recovered', identifier),
            ('prepared', 'prepared', identifier),
        ):
            value = item.get(field)
            if not value or (field == 'local' and item.get('kind') == 'imported'):
                continue
            path = Path(value)
            parent = (self.workspace / subdir).resolve()
            if (path.parent.resolve() != parent or
                    (name is None and not path.name.startswith(identifier + '-')) or
                    (name is not None and path.name != name)):
                continue
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                raise ValueError('录制副本路径不是普通目录，拒绝删除')
            shutil.rmtree(path)

    async def delete_capture(self, identifier):
        item = self.capture(identifier)
        if self.active or item['status'] in ('preparing', 'recording', 'finalizing'):
            raise ValueError('请先停止当前录制并等待保存完成')
        if self.transfer_tasks or self.jobs.processes:
            raise ValueError('请等待数据准备或标定任务结束')
        remote = item.get('remote')
        if remote and item['status'] != 'replaced':
            if not self.device.info.get('connected') or self.device.info.get('host') != item.get('host'):
                raise ValueError('请先连接录制所在的设备再删除')
            await asyncio.to_thread(self.device.delete_recording, remote, identifier)
        await asyncio.to_thread(self._remove_capture_files, item)
        self.captures.remove(item)
        self.transfers.pop(identifier, None)
        self.save()

    def snapshot(self):
        return dict(device=self.device.info, recorder=self.recorder,
                    frame=self.frame, preview_url=self.device.preview_url,
                    vio=self.vio, vio_transfer=self.vio_transfer,
                    cpu_percent=self.cpu_percent,
                    trajectory=dict(points=self.pose_seq, session=self.pose_session,
                                    available=self.trajectory_path.is_file()),
                    ros=dict(connected=self.ros.running, error=self.ros.error or self.ros_error),
                    error=self.capture_error or self.error or (self.recorder or {}).get('preview_error', ''),
                    captures=list(reversed(self.captures)), jobs=self.jobs.all(), transfers=self.transfers)

    def reset_trajectory(self):
        if self.trajectory_file is not None:
            self.trajectory_file.close()
            self.trajectory_file = None
        self.pose_session = uuid.uuid4().hex
        self.trajectory_path = self.trajectory_dir / (self.pose_session + '.jsonl')
        self.pose_seq = 0
        self.pose_segment = 0
        self.last_pose_stamp = None
        self.last_pose_frame = None
        self.tracking = False
        self.pose_revision += 1

    def on_tracking(self, tracking):
        if not tracking and self.tracking and self.pose_seq:
            self.pose_segment += 1
        self.tracking = tracking
        self.pose_revision += 1

    def on_pose(self, message):
        position = message.pose.position
        orientation = message.pose.orientation
        values = [position.x, position.y, position.z,
                  orientation.x, orientation.y, orientation.z, orientation.w]
        frame = message.header.frame_id
        stamp = (message.header.stamp.sec, message.header.stamp.nanosec)
        if not frame or not all(math.isfinite(value) for value in values):
            return
        if sum(value * value for value in values[3:]) < 1e-12:
            return
        if self.last_pose_frame is not None and (frame != self.last_pose_frame or
                                                stamp < self.last_pose_stamp):
            self.reset_trajectory()
        if stamp == self.last_pose_stamp:
            return
        point = dict(seq=self.pose_seq + 1, segment=self.pose_segment,
                     stamp_sec=stamp[0], stamp_nanosec=stamp[1],
                     position=values[:3], orientation=values[3:], frame=frame)
        try:
            if self.trajectory_file is None:
                self.trajectory_file = self.trajectory_path.open('x', encoding='utf-8', buffering=1)
            self.trajectory_file.write(json.dumps(point, ensure_ascii=False, allow_nan=False,
                                                  separators=(',', ':')) + '\n')
        except OSError as exc:
            self.ros_error = f'轨迹保存失败：{exc}'
            return
        self.pose_seq += 1
        self.last_pose_stamp = stamp
        self.last_pose_frame = frame
        self.tracking = True
        self.pose_revision += 1

    async def poll_cpu(self):
        previous = None
        try:
            while self.device.info.get('connected'):
                try:
                    current = await asyncio.to_thread(self.device.cpu_counters)
                    if previous is not None and current[0] > previous[0]:
                        total = current[0] - previous[0]
                        idle = current[1] - previous[1]
                        self.cpu_percent = round(max(0, min(100, 100 * (total - idle) / total)), 1)
                    previous = current
                except (OSError, RuntimeError, ValueError, IndexError):
                    self.cpu_percent = None
                    previous = None
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise

    async def stop_cpu_poller(self):
        if self.cpu_poller:
            self.cpu_poller.cancel()
            try:
                await self.cpu_poller
            except asyncio.CancelledError:
                pass
            self.cpu_poller = None
        self.cpu_percent = None

    async def poll(self):
        try:
            while True:
                started = asyncio.get_running_loop().time()
                interval = .2
                message = await asyncio.to_thread(self.device.request, {'command': 'status'})
                if self.active and message.get('recording_id') != self.active:
                    await asyncio.sleep(max(0, interval-(asyncio.get_running_loop().time()-started)))
                    continue
                self.recorder = message
                self.frame = {key: message[key] for key in ('sequence', 'width', 'height', 'left_ns', 'right_ns')} if message.get('width') else None
                self.error = ''
                if self.active and message.get('recording_id') == self.active:
                    item = self.capture(self.active)
                    item.update(pairs=message['pairs'], elapsed=message['elapsed'])
                    if message['state'] == 'preview':
                        item.update(status='complete' if message['complete'] else 'failed', error=message.get('error', ''))
                        if not message['complete']:
                            self.capture_error = item['error'] or '录制未通过完整性校验，请重新录制。'
                        self.active = None
                        self.save()
                    else:
                        item['status'] = message['state']
                await asyncio.sleep(max(0, interval-(asyncio.get_running_loop().time()-started)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error, failed = await asyncio.to_thread(self.device.failure, str(exc))
            self.recorder = None
            self.frame = None
            self.device.preview_url = None
            if self.active:
                self.capture(self.active).update(status='failed' if failed else 'interrupted', error=self.error)
                self.active = None
                self.save()

    async def stop_poller(self):
        if self.poller:
            self.poller.cancel()
            try:
                await self.poller
            except asyncio.CancelledError:
                pass
            self.poller = None

    async def retrieve(self, identifier):
        item = self.capture(identifier)
        if self.active:
            raise ValueError('请停止当前录制并等待保存完成后再取回数据')
        if item.get('local'):
            return
        if identifier in self.transfers and self.transfers[identifier]['status'] == 'running':
            return
        if item['status'] != 'complete':
            raise ValueError('只能取回完整录制')
        if not self.device.info.get('connected') or self.device.info.get('host') != item['host']:
            raise ValueError('请连接录制所在的设备')
        progress = {'status': 'running', 'stage': 'download', 'bytes': 0, 'total': 0, 'error': '', 'speed_bps': 0, 'frames': 0, 'pairs': 0, 'cached': False}
        self.transfers[identifier] = progress
        task = asyncio.create_task(self._retrieve(item, progress))
        self.transfer_tasks.add(task)
        task.add_done_callback(self.transfer_tasks.discard)

    async def _retrieve(self, item, progress):
        local = self.workspace / 'captures' / (item['id'] + '-' + uuid.uuid4().hex[:6])
        try:
            started = time.monotonic()
            def update(got, total):
                progress.update(stage='download', bytes=got, total=total, speed_bps=got/max(.001, time.monotonic()-started))
            await asyncio.to_thread(self.device.retrieve, item['remote'], local, update, item['id'])
            try:
                prepared = await self._prepare(item, local, progress)
                report = prepared['report']
            except Exception as exc:
                item.update(failed_download=str(local), preparation_error=str(exc))
                if (local / 'COMPLETE').exists():
                    (local / 'COMPLETE').rename(local / 'INCOMPLETE')
                raise
            item.update(local=str(local), report=report, prepared=prepared['directory'])
            progress.update(status='complete', stage='ready')
            self.save()
        except Exception as exc:
            progress.update(status='failed', error=str(exc))
            self.save()

    async def _prepare(self, item, source, progress):
        return await asyncio.to_thread(preparation.prepare, source,
                                       self.workspace / 'prepared' / item['id'], progress.update)

    async def _import(self, item, source, progress):
        try:
            prepared = await self._prepare(item, source, progress)
            report = prepared['report']
            item.update(status='complete', local=str(source), report=report, pairs=report['pairs'],
                        elapsed=(report['pairs']-1)/report['camera_hz'], prepared=prepared['directory'])
            progress.update(status='complete', stage='ready')
        except Exception as exc:
            item.update(status='failed', error=str(exc))
            progress.update(status='failed', error=str(exc))
        self.save()

    async def recover(self, identifier):
        original = self.capture(identifier)
        if self.active or self.transfer_tasks:
            raise ValueError('请等待当前录制和数据准备结束')
        source = original.get('failed_download') or original.get('local') or original.get('import_source')
        if not source:
            copies = list((self.workspace/'captures').glob(identifier+'-*'))
            if copies:
                source = max(copies, key=lambda p:p.stat().st_mtime_ns)
        if not source:
            raise ValueError('请先完整取回原始录制，才能剔除损坏帧')
        item = dict(id=uuid.uuid4().hex, kind='recovered', local=None, remote=None, status='preparing',
                    pairs=0, elapsed=0, created=datetime.now(timezone.utc).isoformat(), error='',
                    recovery_source_id=identifier, board_width_mm=original.get('board_width_mm'))
        self.captures.append(item)
        progress = dict(status='running', stage='recover', bytes=0, total=0, error='', frames=0, pairs=0, cached=False, dropped=0)
        self.transfers[item['id']] = progress
        task = asyncio.create_task(self._recover(original, item, Path(source), progress))
        self.transfer_tasks.add(task);task.add_done_callback(self.transfer_tasks.discard)
        self.save()
        return item

    async def _recover(self, original, item, source, progress):
        destination = self.workspace/'recovered'/item['id']
        destination.parent.mkdir(exist_ok=True)
        try:
            await asyncio.to_thread(recovery.recover, source, destination, progress.update)
            prepared = await self._prepare(item, destination, progress)
            report = prepared['report']
            item.update(status='complete', local=str(destination), prepared=prepared['directory'], report=report,
                        pairs=report['pairs'], elapsed=(report['pairs']-1)/report['camera_hz'])
            original.update(failed_download=str(source), preparation_error='原记录包含损坏图像，已另存剔除坏帧的副本。')
            self.transfers[original['id']] = dict(status='failed', stage='prepare', bytes=0, total=0, error=original['preparation_error'])
            progress.update(status='complete', stage='ready')
        except Exception as exc:
            item.update(status='failed', error=str(exc));progress.update(status='failed', error=str(exc))
        self.save()

    async def shutdown(self):
        await self.stop_cpu_poller()
        await self.ros.stop()
        if self.trajectory_file is not None:
            self.trajectory_file.close()
            self.trajectory_file = None
        await self.jobs.shutdown()
        if self.active:
            try:
                await asyncio.to_thread(self.device.request, {'command': 'stop'})
                for _ in range(100):
                    if not self.active:
                        break
                    await asyncio.sleep(.2)
            except Exception:
                pass
        if self.transfer_tasks:
            await asyncio.gather(*self.transfer_tasks, return_exceptions=True)
        await self.stop_poller()
        try:
            await asyncio.to_thread(self.device.disconnect)
        except Exception:
            pass


def create_app(workspace=None):
    @asynccontextmanager
    async def lifespan(app):
        state = State(Path(workspace or os.environ.get('TIDE_WORKSPACE', ROOT / 'data')).resolve())
        app.state.console = state
        yield
        await state.shutdown()

    app = FastAPI(title='TideConfigurator', lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '::1', 'testserver'])

    @app.middleware('http')
    async def local_origin(request: Request, call_next):
        origin = request.headers.get('origin')
        if origin and urlparse(origin).hostname not in ('127.0.0.1', 'localhost', '::1'):
            return JSONResponse({'detail': '仅允许本地网页访问'}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ValueError)
    @app.exception_handler(RuntimeError)
    @app.exception_handler(OSError)
    @app.exception_handler(yaml.YAMLError)
    async def operation_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.get('/api/state')
    async def state(request: Request):
        return request.app.state.console.snapshot()

    @app.get('/api/boards')
    async def boards():
        return [board.public() for board in list_boards()]

    @app.post('/api/device/connect')
    async def connect(body: Connect, request: Request):
        service = request.app.state.console
        async with service.operation:
            service.error = ''
            try:
                info = await asyncio.to_thread(service.device.connect, body.host, body.port, body.username,
                                               body.password.get_secret_value(), body.board)
                try:
                    service.vio = await asyncio.to_thread(service.device.vio_status)
                except (OSError, RuntimeError):
                    service.vio = {'state': 'unknown'}
                service.cpu_poller = asyncio.create_task(service.poll_cpu())
                if service.vio['state'] in ('running', 'external'):
                    try:
                        await service.ros.start()
                    except (OSError, RuntimeError) as exc:
                        service.ros_error = str(exc)
                return info
            except paramiko_error_types() as exc:
                raise HTTPException(400, f'SSH 连接失败：{type(exc).__name__}') from exc

    @app.post('/api/device/disconnect')
    async def disconnect(request: Request):
        service = request.app.state.console
        async with service.operation:
            if service.active or service.transfer_tasks:
                raise ValueError('请等待录制保存和数据取回完成')
            await service.stop_poller()
            await service.stop_cpu_poller()
            await service.ros.stop()
            await asyncio.to_thread(service.device.disconnect)
            service.recorder = service.frame = None
            service.vio = {'state': 'disconnected'}
            service.on_tracking(False)
            return {'ok': True}

    @app.get('/api/camera/settings')
    async def camera_settings(request: Request):
        service = request.app.state.console
        if not service.device.info.get('connected'):
            raise ValueError('请先连接设备')
        return await asyncio.to_thread(service.device.camera_settings)

    @app.post('/api/camera/settings')
    async def camera_settings_save(body: CameraSettings, request: Request):
        service = request.app.state.console
        async with service.operation:
            if not service.device.info.get('connected'):
                raise ValueError('请先连接设备')
            if service.recorder or service.active:
                raise ValueError('请先关闭相机预览再修改曝光上限')
            service.vio = await asyncio.to_thread(service.device.vio_status)
            if service.vio['state'] != 'stopped':
                raise ValueError('请先停止 VIO 再修改曝光上限')
            return await asyncio.to_thread(service.device.camera_settings_save, body.exposure_max_us)

    @app.get('/api/vio/status')
    async def vio_status(request: Request):
        service = request.app.state.console
        if not service.device.info.get('connected'):
            return {'state': 'disconnected'}
        if service.vio_transfer.get('status') == 'running':
            return service.vio
        service.vio = await asyncio.to_thread(service.device.vio_status)
        return service.vio

    @app.post('/api/vio/start')
    async def vio_start(request: Request):
        service = request.app.state.console
        async with service.operation:
            if service.active or service.transfer_tasks:
                raise ValueError('请先停止录制并等待数据取回完成')
            await service.ros.start()
            service.ros_error = ''
            try:
                await service.stop_poller()
                await asyncio.to_thread(service.device.close_session)
                service.recorder = service.frame = None
                service.reset_trajectory()
                service.vio = await asyncio.to_thread(service.device.vio_start)
            except Exception:
                await service.ros.stop()
                raise
            return service.vio

    @app.post('/api/vio/stop')
    async def vio_stop(request: Request):
        service = request.app.state.console
        async with service.operation:
            service.vio = await asyncio.to_thread(service.device.vio_stop)
            await service.ros.stop()
            service.on_tracking(False)
            return service.vio

    @app.post('/api/vio/deploy')
    async def vio_deploy(request: Request):
        service = request.app.state.console
        async with service.operation:
            if not service.device.info.get('connected'):
                raise ValueError('请先连接设备')
            if service.active or service.transfer_tasks:
                raise ValueError('请先停止录制并等待数据取回完成')
            service.vio = await asyncio.to_thread(service.device.vio_status)
            if service.vio['state'] != 'stopped':
                raise ValueError('请先停止 VIO 再部署')
            try:
                declared = int(request.headers.get('content-length', '0'))
            except ValueError as exc:
                raise ValueError('上传文件长度无效') from exc
            if declared > MAX_ARCHIVE_BYTES:
                raise ValueError('SchurVINS 安装包超过 512 MiB')
            transfer = service.vio_transfer = dict(status='running', stage='browser_upload',
                                                    bytes=0, total=declared, error='')
            filename = None
            try:
                with tempfile.NamedTemporaryFile(dir=service.workspace, prefix='.vio-upload-',
                                                 delete=False) as output:
                    filename = Path(output.name)
                    async for chunk in request.stream():
                        transfer['bytes'] += len(chunk)
                        if transfer['bytes'] > MAX_ARCHIVE_BYTES:
                            raise ValueError('SchurVINS 安装包超过 512 MiB')
                        output.write(chunk)
                if not transfer['bytes']:
                    raise ValueError('请选择 SchurVINS 安装包')
                transfer.update(stage='verify', bytes=0, total=0)
                await asyncio.to_thread(validate_package, filename, service.device.board)
                def progress(stage, sent, total):
                    transfer.update(stage=stage, bytes=sent, total=total)
                result = await asyncio.to_thread(service.device.vio_deploy, filename, progress)
                transfer.update(status='complete', stage='complete', bytes=0, total=0,
                                backup=result['backup'])
                return result
            except Exception as exc:
                transfer.update(status='failed', error=str(exc))
                raise
            finally:
                if filename:
                    filename.unlink(missing_ok=True)

    @app.post('/api/preview')
    async def preview(body: Preview, request: Request):
        if (body.width, body.height) not in recording.RESOLUTIONS:
            raise ValueError('不支持的采集尺寸')
        service = request.app.state.console
        async with service.operation:
            service.vio = await asyncio.to_thread(service.device.vio_status)
            if service.vio['state'] != 'stopped':
                raise ValueError('VIO 正在运行，请先停止 VIO 再启动预览')
            if service.transfer_tasks and service.device.preview_url is None:
                raise ValueError('请等待数据取回完成后再启动预览')
            message = await asyncio.to_thread(service.device.preview, body.width, body.height)
            service.error = service.capture_error = ''
            service.recorder = message
            if not service.poller or service.poller.done():
                service.poller = asyncio.create_task(service.poll())
            return message

    @app.post('/api/recordings')
    async def start(body: Start, request: Request):
        service = request.app.state.console
        if body.kind == 'calibration':
            target.configuration(body.board_width_mm)
        async with service.operation:
            if service.active or service.transfer_tasks:
                raise ValueError('请等待当前录制和数据取回完成')
            output, response = await asyncio.to_thread(service.device.start, body.kind, body.output, body.seconds)
            for item in service.captures:
                if item.get('host') == service.device.info['host'] and item.get('remote') == output and not item.get('local'):
                    item['status'] = 'replaced'
            capture = dict(id=response['recording_id'], kind=body.kind, remote=output, local=None,
                           host=service.device.info['host'], status='preparing', pairs=0, elapsed=0,
                           created=datetime.now(timezone.utc).isoformat(), error='',
                           board_width_mm=body.board_width_mm if body.kind == 'calibration' else None)
            service.captures.append(capture)
            service.capture_error = ''
            service.active = capture['id']
            service.recorder = response
            service.save()
            return capture

    @app.post('/api/recordings/stop')
    async def stop(request: Request):
        return await asyncio.to_thread(request.app.state.console.device.request, {'command': 'stop'})

    @app.post('/api/captures/import')
    async def import_capture(body: ImportCapture, request: Request):
        service = request.app.state.console
        source = Path(body.path).expanduser().resolve(strict=True)
        recording.metadata(source)
        capture = dict(id=uuid.uuid4().hex, kind='imported', local=None, remote=None, status='preparing',
                       pairs=0, elapsed=0, import_source=str(source),
                       created=datetime.now(timezone.utc).isoformat(), error='')
        service.captures.append(capture)
        progress = dict(status='running', stage='prepare', bytes=0, total=0, error='', speed_bps=0, frames=0, pairs=0, cached=False)
        service.transfers[capture['id']] = progress
        task = asyncio.create_task(service._import(capture, source, progress))
        service.transfer_tasks.add(task)
        task.add_done_callback(service.transfer_tasks.discard)
        service.save()
        return capture

    @app.post('/api/captures/{identifier}/retrieve')
    async def retrieve(identifier: str, request: Request):
        await request.app.state.console.retrieve(identifier)
        return {'ok': True}

    @app.post('/api/captures/{identifier}/recover')
    async def recover_capture(identifier: str, request: Request):
        return await request.app.state.console.recover(identifier)

    @app.delete('/api/captures/{identifier}')
    async def delete_capture(identifier: str, request: Request):
        service = request.app.state.console
        async with service.operation:
            await service.delete_capture(identifier)
        return {'ok': True}

    @app.post('/api/calibrations')
    async def calibrate(body: Calibrate, request: Request):
        service = request.app.state.console
        async with service.operation:
            item = service.capture(body.capture_id)
            if item['status'] != 'complete' or not item.get('local'):
                raise ValueError('请先停止录制、等待保存完成并将数据取回电脑')
            return await service.jobs.start(Path(item['local']), body.lens, body.noise, body.board_width_mm, item.get('prepared'))

    @app.post('/api/calibrations/{identifier}/cancel')
    async def cancel(identifier: str, request: Request):
        await request.app.state.console.jobs.cancel(identifier)
        return {'ok': True}

    @app.delete('/api/calibrations/{identifier}')
    async def delete_calibration(identifier: str, request: Request):
        await asyncio.to_thread(request.app.state.console.jobs.delete, identifier)
        return {'ok': True}

    @app.post('/api/calibrations/{identifier}/apply')
    async def apply_calibration(identifier: str, request: Request):
        service = request.app.state.console
        async with service.operation:
            if not service.device.info.get('connected'):
                raise ValueError('请先连接设备再应用标定配置')
            service.vio = await asyncio.to_thread(service.device.vio_status)
            if service.vio['state'] != 'stopped':
                raise ValueError('请先停止 VIO 再应用标定配置')
            folder = service.jobs.folder(identifier)
            job = json.loads((folder / 'job.json').read_text())
            result_dir = folder / 'result'
            config = result_dir / 'schurvins.yaml'
            marker = result_dir / 'COMPLETE'
            if (job.get('id') != identifier or job.get('status') != 'complete' or
                    result_dir.is_symlink() or marker.is_symlink() or
                    not marker.is_file() or config.is_symlink() or not config.is_file()):
                raise ValueError('只有已完成的标定结果可以应用到设备')
            parsed = yaml.safe_load(config.read_text())
            if (not isinstance(parsed, dict) or
                    not isinstance(parsed.get('cameras'), list) or
                    len(parsed['cameras']) != 2 or
                    not isinstance(parsed.get('imu_params'), dict)):
                raise ValueError('标定结果中的 VIO 配置无效')
            result = await asyncio.to_thread(service.device.vio_apply, config)
            service.vio = await asyncio.to_thread(service.device.vio_status)
            return {'job_id': identifier, **result}

    @app.get('/api/calibrations/{identifier}/log')
    async def log(identifier: str, request: Request):
        folder = request.app.state.console.jobs.folder(identifier)
        path = folder / 'process.log'
        chunks = [tail(path, 10000)] if path.exists() else []
        for stage in ('cameras', 'imucam'):
            path = folder / f'result/{stage}.log'
            if path.exists():
                chunks.append(f'\n[{stage}]\n' + tail(path, 12000))
        return {'text': '\n'.join(chunks)}

    @app.get('/api/calibrations/{identifier}/files/{name}')
    async def artifact(identifier: str, name: str, request: Request):
        allowed = {'schurvins.yaml', 'report.md', 'calibration.json', 'dataset-camchain.yaml', 'dataset-imu.yaml',
                   'dataset-camchain-imucam.yaml', 'dataset-report-cam.pdf', 'dataset-report-imucam.pdf',
                   'dataset-results-cam.txt', 'dataset-results-imucam.txt'}
        if name not in allowed:
            raise HTTPException(404)
        folder = request.app.state.console.jobs.folder(identifier)
        path = folder / 'result' / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, filename=name, content_disposition_type='inline' if name.endswith('.pdf') else 'attachment')

    @app.get('/api/noise-template.yaml')
    async def noise_template():
        return FileResponse(ROOT / 'calibration/imu-noise.yaml.example', filename='imu-noise.yaml')

    @app.get('/api/target')
    async def target_specification():
        return target.specification()

    @app.get('/api/target.svg')
    async def target_svg():
        return Response(target.svg(), media_type='image/svg+xml')

    @app.websocket('/api/events')
    async def events(websocket: WebSocket):
        origin = websocket.headers.get('origin')
        if origin and urlparse(origin).hostname not in ('127.0.0.1', 'localhost', '::1'):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                service = websocket.app.state.console
                await asyncio.wait_for(websocket.send_json(service.snapshot()), 3)
                await asyncio.sleep(.2)
        except (WebSocketDisconnect, OSError, TimeoutError, RuntimeError):
            pass

    @app.websocket('/api/vio/trajectory')
    async def trajectory(websocket: WebSocket):
        origin = websocket.headers.get('origin')
        if origin and urlparse(origin).hostname not in ('127.0.0.1', 'localhost', '::1'):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        service = websocket.app.state.console
        session = None
        reader = None
        revision = -1
        last_sent = 0.0
        try:
            while True:
                reset = session != service.pose_session
                if reset:
                    if reader is not None:
                        reader.close()
                    session = service.pose_session
                    reader = None
                if reader is None and service.trajectory_path.is_file():
                    reader = service.trajectory_path.open('r', encoding='utf-8')
                points = []
                if reader is not None:
                    for _ in range(512):
                        line = reader.readline()
                        if not line:
                            break
                        points.append(json.loads(line))
                if reset or points or service.pose_revision != revision or time.monotonic() - last_sent >= 2:
                    await asyncio.wait_for(websocket.send_json(dict(
                        reset=reset, points=points, tracking=service.tracking,
                        frame=service.last_pose_frame, session=session)), 3)
                    revision = service.pose_revision
                    last_sent = time.monotonic()
                await asyncio.sleep(0 if len(points) == 512 else .05)
        except (WebSocketDisconnect, OSError, TimeoutError, RuntimeError):
            pass
        finally:
            if reader is not None:
                reader.close()

    @app.get('/api/vio/trajectory/download')
    async def trajectory_download(request: Request):
        service = request.app.state.console
        if not service.trajectory_path.is_file():
            raise HTTPException(404, '当前没有可下载的 VIO 轨迹')
        return FileResponse(service.trajectory_path, filename=f'vio-{service.pose_session}.jsonl')

    dist = Path(os.environ.get('TIDE_FRONTEND_DIST', ROOT / 'frontend/dist'))
    if dist.is_dir():
        app.mount('/', StaticFiles(directory=dist, html=True), name='frontend')
    return app


def paramiko_error_types():
    from paramiko import SSHException
    return (SSHException,)


app = create_app()
