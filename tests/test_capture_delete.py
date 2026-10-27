"""Deleting a recording must respect capture identity and file ownership."""
import io
import stat
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from tide_configurator.app import create_app
from tide_configurator.device import Device


def test_delete_owned_capture_and_keep_imported_source(tmp_path):
    workspace = tmp_path / 'workspace'
    imported = tmp_path / 'external'
    imported.mkdir()
    (imported / 'frames.bin').write_bytes(b'external')
    with TestClient(create_app(workspace)) as client:
        state = client.app.state.console
        local_id, imported_id = 'a'*32, 'b'*32
        local = workspace / 'captures' / (local_id + '-copy')
        local.mkdir()
        (local / 'frames.bin').write_bytes(b'owned')
        cache = workspace / 'prepared' / local_id
        cache.mkdir(parents=True)
        (cache / 'dataset.bag').write_bytes(b'cache')
        state.captures.extend([
            dict(id=local_id, kind='dataset', status='complete', local=str(local), prepared=str(cache)),
            dict(id=imported_id, kind='imported', status='complete', local=str(imported)),
        ])
        state.save()
        assert client.delete(f'/api/captures/{local_id}').status_code == 200
        assert not local.exists() and not cache.exists()
        assert client.delete(f'/api/captures/{imported_id}').status_code == 200
        assert (imported / 'frames.bin').read_bytes() == b'external'
        assert client.get('/api/state').json()['captures'] == []
        assert client.delete(f'/api/captures/{local_id}').status_code == 400


def test_delete_remote_requires_right_device_and_idle_state(tmp_path):
    with TestClient(create_app(tmp_path / 'workspace')) as client:
        state = client.app.state.console
        identifier = 'c'*32
        state.captures.append(dict(id=identifier, kind='dataset', status='complete',
                                   host='device', remote='/mnt/data/tide-configurator/dataset-1', local=None))
        calls = []
        state.device.delete_recording = lambda path, key: calls.append((path, key))
        assert client.delete(f'/api/captures/{identifier}').status_code == 400
        state.device.info = {'connected': True, 'host': 'another-device'}
        assert client.delete(f'/api/captures/{identifier}').status_code == 400
        state.device.info['host'] = 'device'
        state.active = 'd'*32
        assert client.delete(f'/api/captures/{identifier}').status_code == 400
        state.active = None
        assert client.delete(f'/api/captures/{identifier}').status_code == 200
        assert calls == [('/mnt/data/tide-configurator/dataset-1', identifier)]


class FakeSftp:
    def __init__(self, id_in_file, names):
        self.id_in_file = id_in_file
        self.names = names
        self.removed = []
    def lstat(self, path):
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700)
    def open(self, path, mode):
        return io.BytesIO(('recording_id: ' + self.id_in_file).encode())
    def listdir_attr(self, path):
        return [SimpleNamespace(filename=name, st_mode=stat.S_IFREG | 0o600) for name in self.names]
    def remove(self, path):
        self.removed.append(path)
    def rmdir(self, path):
        self.removed.append(path)
    def close(self):
        pass


def test_device_refuses_foreign_or_unexpected_recording(tmp_path):
    device = Device(tmp_path)
    directory = '/mnt/data/tide-configurator/dataset-1'
    sftp = FakeSftp('other-id', ['camera-config.yaml', 'frames.bin'])
    device.client = SimpleNamespace(open_sftp=lambda: sftp)
    device.info = {'connected': True}
    with pytest.raises(ValueError, match='编号不匹配'):
        device.delete_recording(directory, 'a'*32)
    assert not sftp.removed
    sftp.id_in_file = 'a'*32
    sftp.names.append('unrelated.txt')
    with pytest.raises(ValueError, match='未知文件'):
        device.delete_recording(directory, 'a'*32)
    assert not sftp.removed
    sftp.names.remove('unrelated.txt')
    device.delete_recording(directory, 'a'*32)
    assert sftp.removed == [directory+'/camera-config.yaml', directory+'/frames.bin', directory]
    with pytest.raises(ValueError):
        device.delete_recording('/mnt/data/tide-configurator', 'a'*32)
