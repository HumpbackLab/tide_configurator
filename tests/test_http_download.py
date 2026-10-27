import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import yaml

from tide_configurator.device import Device


@pytest.mark.parametrize('truncate', [False, True])
def test_files_use_http_and_reject_truncation(tmp_path, truncate):
    identifier = 'b'*32
    files = {'frames.bin': b'compressed-recording'*100,
             'imu.csv': b'123,1,2,3,4,5,6\n', 'metadata.yaml': b'complete: true\n',
             'camera-config.yaml': yaml.safe_dump({'recording_id': identifier}).encode(),
             'imu-source.yaml': b'raw_si: true\n'}
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            requests.append(self.path)
            data = files[self.path.rsplit('/', 1)[-1]]
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data[:-1] if truncate else data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        device = Device(tmp_path)
        device.client = MagicMock()
        device.remote = '/tmp/native-session'
        device.http_base = f'http://127.0.0.1:{server.server_port}/session'
        calls = []
        def control(command):
            calls.append(command)
            if command['command'] == 'download':
                return dict(files=[dict(name=n, size=len(data)) for n, data in files.items()])
            return {'ok': True}
        device.request = control
        destination = tmp_path/'capture'
        progress = []
        if truncate:
            with pytest.raises((OSError, RuntimeError)):
                device.retrieve('/recording', destination, lambda a,b:progress.append((a,b)), identifier)
            assert (destination/'INCOMPLETE').exists()
            assert not (destination/'COMPLETE').exists()
        else:
            device.retrieve('/recording', destination, lambda a,b:progress.append((a,b)), identifier)
            assert (destination/'COMPLETE').exists()
            assert all((destination/name).read_bytes()==data for name, data in files.items())
            assert progress[-1] == (sum(map(len, files.values())),)*2
        assert calls[-1]['command'] == 'download_release'
        assert all('/download/' in path for path in requests)
        device.client.open_sftp.assert_not_called()
    finally:
        server.shutdown(); server.server_close(); thread.join()
