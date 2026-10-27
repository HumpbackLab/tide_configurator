import json
import asyncio
import threading
import time
from unittest.mock import MagicMock
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest
import yaml

from tide_calibration.recording import HEADER, MAGIC
from tide_configurator.app import create_app, State
from tide_configurator.jobs import Jobs
from tide_configurator.device import Device


def capture(path):
    path.mkdir()
    (path / 'COMPLETE').touch()
    (path / 'metadata.yaml').write_text(yaml.safe_dump(dict(schema='tidesense-dataset-v2', complete=True, pairs=3, imu_samples=41)))
    with (path / 'frames.bin').open('wb') as stream:
        stream.write(MAGIC)
        for i in range(3):
            t = 10**9+i*50_000_000
            stream.write(HEADER.pack(0x54444341, 1, 640, 400, i+1, t, t+2_000_000, i, i, 1, 0))
            stream.write(bytes(2*640*400))
    (path / 'imu.csv').write_text(''.join(f'{950_000_000+i*5_000_000},0,0,0,0,0,9.8\n' for i in range(41)))


def test_local_origin_and_screen_target(tmp_path):
    with TestClient(create_app(tmp_path / 'workspace')) as client:
        assert client.get('/api/boards').json() == [
            {'id': 'tidesense', 'model': 'TideSense', 'default_host': '192.168.123.99'}]
        response = client.get('/api/state')
        assert response.status_code == 200
        assert not response.json()['device']['connected']
        assert client.post('/api/device/disconnect', headers={'Origin': 'https://example.com'}).status_code == 403
        assert client.get('/api/state', headers={'Host': 'untrusted.example'}).status_code == 400
        spec = client.get('/api/target').json()
        assert spec['cols'] == 6 and spec['rows'] == 4
        assert client.get('/api/target.svg').headers['content-type'].startswith('image/svg+xml')
        assert 'guidance' not in response.json()
        with client.websocket_connect('/api/events') as socket:
            assert socket.receive_json()['captures'] == []


def test_import_is_readonly_persists_and_rejects_incomplete(tmp_path):
    source = tmp_path / 'capture'
    capture(source)
    original = {p.name: p.read_bytes() for p in source.iterdir()}
    workspace = tmp_path / 'workspace'
    with TestClient(create_app(workspace)) as client:
        response = client.post('/api/captures/import', json={'path': str(source)})
        assert response.status_code == 200, response.text
        item = response.json()
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            item = next(c for c in client.get('/api/state').json()['captures'] if c['id']==item['id'])
            if item['status'] != 'preparing':break
            time.sleep(.02)
        assert item.get('prepared')
        assert item['pairs'] == 3 and item['status'] == 'complete'
        response = client.post('/api/calibrations', json={'capture_id': item['id'], 'lens': 'wide', 'noise': 'update_rate: null', 'board_width_mm': 259.2})
        assert response.status_code == 400
        assert not list((workspace / 'jobs').iterdir())
    assert original == {p.name: p.read_bytes() for p in source.iterdir()}
    with TestClient(create_app(workspace)) as client:
        assert client.get('/api/state').json()['captures'][0]['id'] == item['id']
        (source / 'INCOMPLETE').touch()
        response = client.post('/api/captures/import', json={'path': str(source)})
        assert response.status_code == 400


def test_recording_path_and_status_are_forwarded_to_device(tmp_path):
    with TestClient(create_app(tmp_path / 'workspace')) as client:
        state = client.app.state.console
        state.device.info = {'connected': True, 'host': 'device'}
        calls = []
        def start(kind, output, seconds):
            calls.append((kind, output, seconds))
            return output, {'state': 'preparing', 'recording_id': 'a'*32}
        state.device.start = start
        response = client.post('/api/recordings', json={'kind': 'dataset', 'output': '/mnt/data/custom name', 'seconds': 0})
        assert response.status_code == 200
        assert calls == [('dataset', '/mnt/data/custom name', 0)]
        assert response.json()['status'] == 'preparing'
        assert client.post('/api/recordings', json={'kind': 'dataset'}).status_code == 400
        state.active = None


def test_artifact_allowlist_and_job_identifier(tmp_path):
    with TestClient(create_app(tmp_path / 'workspace')) as client:
        assert client.get('/api/calibrations/not-an-id/log').status_code == 400
        assert client.get('/api/calibrations/' + 'a'*32 + '/files/imu.yaml').status_code == 404


def test_running_job_becomes_interrupted_after_server_restart(tmp_path):
    folder = tmp_path / 'jobs' / ('a'*32)
    folder.mkdir(parents=True)
    (folder / 'job.json').write_text(json.dumps(dict(id='a'*32, status='running', created='2026-01-01')))
    jobs = Jobs(tmp_path)
    assert jobs.all()[0]['status'] == 'interrupted'


def test_stale_status_cannot_complete_or_fail_a_new_recording(tmp_path):
    async def run():
        state = State(tmp_path)
        state.active = 'a'*32
        state.captures.append(dict(id=state.active, status='preparing', pairs=0, elapsed=0))
        calls = 0
        def request(command):
            nonlocal calls
            calls += 1
            if calls == 1:
                return dict(state='preview', recording_id='old', pairs=81, elapsed=4, complete=True, error='')
            return dict(state='recording', recording_id='a'*32, pairs=5, elapsed=.2, complete=False, error='')
        state.device.request = request
        task = asyncio.create_task(state.poll())
        await asyncio.sleep(.65)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert state.active == 'a'*32
        assert state.captures[0]['status'] == 'recording'
        assert state.captures[0]['pairs'] == 5
    asyncio.run(run())


def test_preview_failure_keeps_recording_controls_and_state(tmp_path):
    async def run():
        state = State(tmp_path)
        state.active = 'b'*32
        state.captures.append(dict(id=state.active, status='recording', pairs=1, elapsed=0))
        def request(command):
            assert command == {'command': 'status'}
            return dict(state='recording', recording_id='b'*32, pairs=8, elapsed=.35, complete=False, error='', preview_error='hardware preview stalled')
        state.device.request = request
        task = asyncio.create_task(state.poll())
        await asyncio.sleep(.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert state.recorder['state'] == 'recording'
        assert state.active == 'b'*32
        assert state.captures[0]['pairs'] == 8
        assert state.snapshot()['error'] == 'hardware preview stalled'
        assert state.device.preview_url is None
    asyncio.run(run())


def test_start_during_status_request_does_not_accept_old_recording_state(tmp_path):
    async def run():
        state = State(tmp_path)
        entered, release = threading.Event(), threading.Event()
        calls = 0
        def request(command):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                release.wait(2)
                return dict(state='preview', recording_id='old', sequence=1, pairs=81, elapsed=4, complete=True, error='')
            return dict(state='recording', recording_id='c'*32, pairs=3, elapsed=.1, complete=False, error='')
        state.device.request = request
        task = asyncio.create_task(state.poll())
        assert await asyncio.to_thread(entered.wait, 2)
        state.active = 'c'*32
        state.captures.append(dict(id=state.active, status='preparing', pairs=0, elapsed=0))
        release.set()
        await asyncio.sleep(.65)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert state.active == 'c'*32
        assert state.captures[0]['status'] == 'recording'
        assert state.frame is None
    asyncio.run(run())


def test_retrieval_refuses_a_replaced_calibration_directory(tmp_path):
    device = Device(tmp_path)
    device.client = MagicMock()
    device.remote = '/tmp/session'
    def request(command):
        if command['command'] == 'download':
            raise RuntimeError('设备上的临时录制已被替换')
        return {'ok': True, 'state': 'preview'}
    device.request = request
    destination = tmp_path / 'download'
    with pytest.raises(RuntimeError, match='已被替换'):
        device.retrieve('/mnt/data/tide-configurator/calibration', destination, lambda a, b: None, 'a'*32)
    assert not destination.exists()
    device.client.open_sftp.assert_not_called()


def test_recorder_exit_marks_capture_failed_with_native_reason(tmp_path):
    async def run():
        state = State(tmp_path)
        state.active = 'e'*32
        state.captures.append(dict(id=state.active, status='recording', pairs=176, elapsed=8.8))
        state.device.request = MagicMock(side_effect=RuntimeError('录制器连接已关闭'))
        state.device.preview_url = 'http://device/old-preview'
        reason = '录制失败：图像压缩或写盘速度跟不上采集，写入队列已满；数据不完整，请重新录制。'
        state.device.failure = lambda fallback: (reason, True)
        await state.poll()
        assert state.error == reason
        assert state.snapshot()['preview_url'] is None
        assert state.active is None
        assert state.captures[0]['status'] == 'failed'
        assert state.captures[0]['error'] == reason
    asyncio.run(run())


def test_failed_download_validation_reverts_completion_marker(tmp_path):
    async def run():
        state = State(tmp_path)
        item = dict(id='f'*32, status='complete', remote='/source', local=None)
        def retrieve(remote, local, progress, identifier):
            capture(local)
            (local / 'frames.bin').write_bytes(b'corrupt archive')
        state.device.retrieve = retrieve
        progress = dict(status='running')
        await state._retrieve(item, progress)
        assert progress['status'] == 'failed'
        destination = next((tmp_path / 'captures').iterdir())
        assert (destination / 'INCOMPLETE').is_file()
        assert not (destination / 'COMPLETE').exists()
        assert item['local'] is None
    asyncio.run(run())


def test_finalization_failure_survives_healthy_status(tmp_path):
    async def run():
        state = State(tmp_path)
        identifier = 'd'*32
        reason = 'Stored frame #730 bytes differ from verified encoded packet'
        state.active = identifier
        state.captures.append(dict(id=identifier, status='finalizing', pairs=20, elapsed=1))
        sequence = 0
        def request(command):
            nonlocal sequence
            sequence += 1
            return dict(state='preview', recording_id=identifier, pairs=20, elapsed=1,
                        complete=False, error=reason if sequence == 1 else '', sequence=sequence,
                        width=640, height=400, left_ns=sequence, right_ns=sequence)
        state.device.request = request
        task = asyncio.create_task(state.poll())
        try:
            await asyncio.sleep(.85)
            assert sequence >= 2
            assert state.error == ''
            assert state.snapshot()['error'] == reason
            assert state.captures[0]['status'] == 'failed'
            assert state.active is None
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())
