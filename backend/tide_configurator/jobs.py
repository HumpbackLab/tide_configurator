"""Persisted calibration jobs; each run owns its process group and result directory."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import shutil
import stat
import sys
import uuid

import yaml

from tide_calibration.pipeline import noise_config
from .target import configuration

ROOT = Path(__file__).resolve().parents[2]


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    temporary.replace(path)


def tail(path, limit):
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size-limit))
        return stream.read().decode(errors='replace')


class Jobs:
    def __init__(self, workspace):
        self.directory = workspace / 'jobs'
        self.directory.mkdir(exist_ok=True)
        self.processes = {}
        self.tasks = set()
        for path in self.directory.glob('*/job.json'):
            job = json.loads(path.read_text())
            if job['status'] == 'running':
                job.update(status='interrupted', error='服务已重启；请创建新的标定任务。')
                write_json(path, job)

    def all(self):
        jobs = []
        for path in self.directory.glob('*/job.json'):
            job = json.loads(path.read_text())
            state = path.parent / 'result/calibration.json'
            if state.exists():
                try:
                    result = json.loads(state.read_text())
                    job['stage'] = result.get('stage', 'preflight')
                    job['result'] = result
                except json.JSONDecodeError:
                    pass
            jobs.append(job)
        return sorted(jobs, key=lambda job: job['created'], reverse=True)

    def folder(self, identifier):
        if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('无效的任务编号')
        folder = self.directory / identifier
        if not folder.is_dir() or not stat.S_ISDIR(folder.lstat().st_mode):
            raise ValueError('任务不存在')
        return folder

    def delete(self, identifier):
        if identifier in self.processes:
            raise ValueError('请先取消正在运行的标定任务')
        folder = self.folder(identifier)
        job = json.loads((folder / 'job.json').read_text())
        if job.get('id') != identifier or job.get('status') == 'running':
            raise ValueError('标定任务状态不允许删除')
        shutil.rmtree(folder)

    async def start(self, source, lens, noise, board_width_mm, prepared=None):
        if self.processes:
            raise ValueError('已有标定任务运行中，请等待完成或取消')
        target = configuration(board_width_mm)
        identifier = uuid.uuid4().hex
        folder = self.directory / identifier
        folder.mkdir()
        (folder / 'imu.yaml').write_text(noise)
        try:
            noise_config(folder / 'imu.yaml')
        except Exception:
            (folder / 'imu.yaml').unlink()
            folder.rmdir()
            raise
        (folder / 'target.yaml').write_text(yaml.safe_dump(target, sort_keys=False))
        job = dict(id=identifier, source=str(source), lens=lens, status='running', stage='preflight',
                   created=datetime.now(timezone.utc).isoformat(), error='',
                   board_width_mm=board_width_mm, target=target)
        write_json(folder / 'job.json', job)
        command = [sys.executable, '-u', str(ROOT / 'calibration/calibrate.py'), str(source),
                   '--imu', str(folder / 'imu.yaml'), '--lens', lens,
                   '--target', str(folder / 'target.yaml'), '--output', str(folder / 'result')]
        if prepared:
            command += ['--prepared', str(prepared)]
        log = (folder / 'process.log').open('wb')
        try:
            process = await asyncio.create_subprocess_exec(*command, stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except Exception:
            log.close()
            job.update(status='failed', error='无法启动标定进程')
            write_json(folder / 'job.json', job)
            raise
        self.processes[identifier] = process
        task = asyncio.create_task(self._watch(identifier, process, log))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    async def _watch(self, identifier, process, log):
        code = await process.wait()
        log.close()
        folder = self.folder(identifier)
        job = json.loads((folder / 'job.json').read_text())
        if job['status'] != 'cancelled':
            complete = (folder / 'result/COMPLETE').is_file()
            job.update(status='complete' if code == 0 and complete else 'failed', returncode=code)
            if job['status'] == 'failed':
                camera_log = folder / 'result/cameras.log'
                if camera_log.exists() and 'No corners could be extracted' in tail(camera_log, 20000):
                    job['error'] = '没有提取到有效的标定板角点。请检查标定板、图像清晰度和目标参数，补充录制后重新标定。'
                else:
                    job['error'] = '标定未完成，请查看求解日志了解失败原因。'
            write_json(folder / 'job.json', job)
        self.processes.pop(identifier, None)

    async def cancel(self, identifier):
        process = self.processes.get(identifier)
        if not process:
            raise ValueError('任务已结束')
        folder = self.folder(identifier)
        job = json.loads((folder / 'job.json').read_text())
        job.update(status='cancelled', error='用户取消')
        write_json(folder / 'job.json', job)
        try:
            os.killpg(process.pid, signal.SIGINT)
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=12)
        except asyncio.TimeoutError:
            os.killpg(process.pid, signal.SIGTERM)
            await process.wait()
        except ProcessLookupError:
            pass

    async def shutdown(self):
        for identifier in list(self.processes):
            await self.cancel(identifier)
