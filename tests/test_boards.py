import pytest

from tide_configurator.boards import list_boards, load_board
from tide_configurator.device import Device


def test_tidesense_programs_are_device_paths(tmp_path):
    board = load_board('tidesense')
    assert [item.id for item in list_boards()] == ['tidesense']
    assert board.recorder == '/usr/local/bin/tidesense_record'
    assert board.vio == '/mnt/data/schurvins-gcc11-static-micro-ros/run.sh'
    assert Device(tmp_path).recorder_path == board.recorder


def test_board_selection_and_invalid_paths(tmp_path):
    with pytest.raises(ValueError, match='无效的板型'):
        load_board('../tidesense')
    with pytest.raises(ValueError, match='不支持的板型'):
        load_board('rv1103')
    directory = tmp_path / 'custom'
    directory.mkdir()
    (directory / 'board.conf').write_text('''model=Custom
architecture=aarch64
default_host=192.0.2.1
recorder=../../bin/recorder
vio=/bin/vio
imu_name=testimu
data_dir=/mnt/data
''')
    with pytest.raises(ValueError, match='recorder.*绝对路径'):
        load_board('custom', tmp_path)
