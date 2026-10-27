"""Runtime settings for each supported device board."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re

ROOT = Path(__file__).resolve().parents[2] / 'boards'
REQUIRED = {'model', 'architecture', 'default_host', 'recorder', 'vio', 'vio_config',
            'vio_output', 'imu_name', 'data_dir'}
OPTIONAL = {'camera_settings'}


@dataclass(frozen=True)
class Board:
    id: str
    model: str
    architecture: str
    default_host: str
    recorder: str
    vio: str
    vio_config: str
    vio_output: str
    imu_name: str
    data_dir: str
    camera_settings: str

    def public(self):
        return {'id': self.id, 'model': self.model, 'default_host': self.default_host}


def load_board(board_id: str, root: Path = ROOT) -> Board:
    if not re.fullmatch(r'[a-z][a-z0-9_]*', board_id):
        raise ValueError('无效的板型名称')
    path = root / board_id / 'board.conf'
    if not path.is_file():
        raise ValueError(f'不支持的板型：{board_id}')
    values = {}
    for number, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ValueError(f'{path}:{number} 配置格式错误')
        key, value = (part.strip() for part in line.split('=', 1))
        if key not in REQUIRED | OPTIONAL or key in values or not value:
            raise ValueError(f'{path}:{number} 未知、重复或空的配置项：{key}')
        values[key] = value
    missing = REQUIRED - values.keys()
    if missing:
        raise ValueError(f'{path} 缺少配置项：{", ".join(sorted(missing))}')
    values.setdefault('camera_settings', values['data_dir'].rstrip('/') + '/tide-configurator/camera.conf')
    for key in ('recorder', 'vio', 'vio_config', 'vio_output', 'data_dir',
                'camera_settings'):
        value = values[key]
        remote = PurePosixPath(value)
        if not remote.is_absolute() or '..' in remote.parts or value == '/':
            raise ValueError(f'{path} 的 {key} 必须是设备上的绝对路径')
    if PurePosixPath(values['vio_config']).parent != PurePosixPath(values['vio']).parent:
        raise ValueError(f'{path} 的 vio_config 必须位于 VIO 安装目录')
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]+', values['default_host']):
        raise ValueError(f'{path} 的 default_host 无效')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+', values['imu_name']):
        raise ValueError(f'{path} 的 imu_name 无效')
    return Board(id=board_id, **values)


def list_boards(root: Path = ROOT) -> list[Board]:
    return [load_board(path.name, root) for path in sorted(root.iterdir())
            if path.is_dir() and (path / 'board.conf').is_file()]
