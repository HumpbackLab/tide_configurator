"""Validate a board-specific SchurVINS release before root extracts it."""
from pathlib import Path, PurePosixPath
import struct
import tarfile

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_CONTENT_BYTES = 1024 * 1024 * 1024


def validate_package(path: Path, board) -> str:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError('SchurVINS 安装包超过 512 MiB')
    root = PurePosixPath(board.vio).parent.name
    required = {f'{root}/run.sh', f'{root}/bin/schurvins_stereo',
                f'{root}/SHA256SUMS', f'{root}/schurvins.yaml'}
    names = set()
    total = 0
    binary = None
    try:
        with tarfile.open(path, 'r:*') as archive:
            for member in archive:
                name = member.name.rstrip('/')
                parts = PurePosixPath(name).parts
                if (not parts or parts[0] != root or '..' in name.split('/') or
                        name != str(PurePosixPath(name)) or
                        PurePosixPath(name).is_absolute() or name in names or
                        not (member.isfile() or member.isdir())):
                    raise ValueError(f'安装包包含不安全或不属于 {root} 的条目：{member.name}')
                names.add(name)
                if member.size < 0:
                    raise ValueError('安装包包含无效的文件长度')
                total += member.size
                if len(names) > 10000 or total > MAX_CONTENT_BYTES:
                    raise ValueError('SchurVINS 安装包内容过大')
                if name == f'{root}/bin/schurvins_stereo':
                    with archive.extractfile(member) as stream:
                        binary = stream.read(20)
    except (tarfile.TarError, EOFError) as exc:
        raise ValueError('无效的 SchurVINS tar 安装包') from exc
    if not required <= names:
        raise ValueError('SchurVINS 安装包缺少运行程序、脚本、配置或校验清单')
    # ELF64 little-endian, e_machine=AArch64. Do not run a host binary on the board.
    if (binary is None or len(binary) < 20 or binary[:6] != b'\x7fELF\x02\x01'
            or struct.unpack_from('<H', binary, 18)[0] != 183):
        raise ValueError('安装包中的 SchurVINS 不是 AArch64 程序')
    with path.open('rb') as stream:
        return 'gz' if stream.read(2) == b'\x1f\x8b' else 'tar'
