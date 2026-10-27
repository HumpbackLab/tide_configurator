import struct
import time
from pathlib import Path

from fastapi.testclient import TestClient
from test_api import capture
import zstandard
from tide_configurator.app import create_app
from tide_calibration import recording


def test_bad_pair_is_recovered_as_a_separate_capture(tmp_path):
    source=tmp_path/'source';capture(source)
    raw=(source/'frames.bin').read_bytes()[len(recording.MAGIC):]
    with (source/'frames.bin').open('wb') as f:
        f.write(b'TIDESENSE_ZST_V1\n')
        size=recording.HEADER.size+2*640*400
        for offset in range(0,len(raw),size):
            block=zstandard.ZstdCompressor(write_checksum=True).compress(raw[offset:offset+size])
            f.write(len(block).to_bytes(4,'little')+block)
    path=source/'frames.bin';data=bytearray(path.read_bytes());offset=len(recording.MAGIC)
    size=struct.unpack_from('<I',data,offset)[0];offset+=4+size
    size=struct.unpack_from('<I',data,offset)[0];data[offset+4+size-1]^=1;path.write_bytes(data)
    original={p.name:p.read_bytes() for p in source.iterdir()}
    with TestClient(create_app(tmp_path/'workspace')) as client:
        state=client.app.state.console
        state.captures.append(dict(id='a'*32,kind='dataset',status='complete',local=None,
                                   failed_download=str(source),pairs=3,elapsed=.1,created='now',error=''))
        response=client.post('/api/captures/'+'a'*32+'/recover')
        assert response.status_code==200,response.text
        identifier=response.json()['id']
        for _ in range(200):
            copy=next(c for c in client.get('/api/state').json()['captures'] if c['id']==identifier)
            if copy['status']!='preparing':break
            time.sleep(.02)
        assert copy['status']=='complete',copy
        assert copy['report']['dropped_camera_pairs']==1 and copy['pairs']==2
        assert Path(copy['local'])!=source and Path(copy['prepared'],'dataset.bag').exists()
    assert original=={p.name:p.read_bytes() for p in source.iterdir()}
