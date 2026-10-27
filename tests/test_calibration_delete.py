import json

from fastapi.testclient import TestClient

from tide_configurator.app import create_app


def test_delete_finished_job_keeps_recording(tmp_path):
    workspace = tmp_path / 'workspace'
    source = tmp_path / 'recording'
    source.mkdir()
    (source / 'frames.bin').write_bytes(b'original')
    identifier = 'a'*32
    folder = workspace / 'jobs' / identifier
    (folder / 'result').mkdir(parents=True)
    (folder / 'job.json').write_text(json.dumps(dict(id=identifier, status='complete', source=str(source), created='2026-01-01')))
    (folder / 'result' / 'schurvins.yaml').write_text('result')
    with TestClient(create_app(workspace)) as client:
        assert client.delete('/api/calibrations/' + identifier).status_code == 200
        assert not folder.exists()
        assert (source / 'frames.bin').read_bytes() == b'original'
        assert client.get('/api/state').json()['jobs'] == []
        assert client.delete('/api/calibrations/' + identifier).status_code == 400


def test_delete_running_or_symlinked_job_is_refused(tmp_path):
    workspace = tmp_path / 'workspace'
    identifier = 'b'*32
    folder = workspace / 'jobs' / identifier
    folder.mkdir(parents=True)
    (folder / 'job.json').write_text(json.dumps(dict(id=identifier, status='running', created='2026-01-01')))
    with TestClient(create_app(workspace)) as client:
        # Startup marks orphaned jobs interrupted; an active process remains protected.
        client.app.state.console.jobs.processes[identifier] = object()
        assert client.delete('/api/calibrations/' + identifier).status_code == 400
        client.app.state.console.jobs.processes.clear()
        assert client.delete('/api/calibrations/' + identifier).status_code == 200
        target = tmp_path / 'external'
        target.mkdir()
        (target / 'job.json').write_text(json.dumps(dict(id=identifier, status='complete', created='2026-01-01')))
        folder.symlink_to(target, target_is_directory=True)
        assert client.delete('/api/calibrations/' + identifier).status_code == 400
        assert target.exists()
