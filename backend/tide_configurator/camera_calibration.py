"""Dark/flat-field FPN analysis and TFPN packaging for the OV9281 cameras."""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile

import numpy as np

MAGIC = b"TFPN"
VERSION = 2
HEADER_SIZE = 24
FLAGS = 7
MIN_GAIN_Q12 = 2048
MAX_GAIN_Q12 = 8192
OFFSET_HALF_SIGNAL = 48.0
SMOOTH_COLUMNS = 33
MAX_GAIN_BUCKETS = 64


def read_meta(path):
    meta = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            meta[key.strip()] = value.strip()
    return meta


def _number(meta, key):
    try:
        value = int(meta[key])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"meta.txt 缺少有效的 {key}") from exc
    if value <= 0:
        raise ValueError(f"meta.txt 的 {key} 必须大于 0")
    return value


def _capture_directories(root, mode):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"{mode} 标定数据尚未采集")
    captures = {}
    for path in root.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        match = re.fullmatch(r"g([0-9]+)", path.name)
        if not match:
            continue
        gain = int(match.group(1))
        meta_path = path / "meta.txt"
        if not meta_path.is_file():
            raise ValueError(f"{path} 缺少 meta.txt")
        meta = read_meta(meta_path)
        if meta.get("mode") != mode:
            raise ValueError(f"{path} 不是 {mode} 采集")
        recorded_gain = _number(meta, "gain_q10")
        if recorded_gain != gain:
            raise ValueError(f"{path} 的目录名与记录增益不一致")
        if gain in captures:
            raise ValueError(f"{mode} 数据包含重复增益 {gain}")
        captures[gain] = (path, meta)
    if len(captures) < 2:
        raise ValueError(f"{mode} 标定至少需要两个增益点")
    if len(captures) > MAX_GAIN_BUCKETS:
        raise ValueError(f"{mode} 标定最多支持 {MAX_GAIN_BUCKETS} 个增益点")
    return captures


def validate_capture(root, mode, expected_gains=None):
    captures = _capture_directories(root, mode)
    gains = sorted(captures)
    if expected_gains is not None and gains != sorted(expected_gains):
        raise ValueError(f"{mode} 数据增益 {gains} 与请求 {sorted(expected_gains)} 不一致")
    shape = None
    frames = None
    for gain in gains:
        path, meta = captures[gain]
        current = (_number(meta, "width"), _number(meta, "height"))
        current_frames = _number(meta, "frames")
        if shape is None:
            shape, frames = current, current_frames
        if current != shape or current_frames != frames:
            raise ValueError(f"{path} 的尺寸或帧数与同组其他增益不一致")
        for cam in (0, 1):
            data = path / f"cam{cam}_cols.f32"
            if not data.is_file() or data.is_symlink():
                raise ValueError(f"{path} 缺少 cam{cam}_cols.f32")
            if data.stat().st_size != current[0] * current_frames * 4:
                raise ValueError(f"{data} 长度与 meta.txt 不一致")
    return {
        "gains": gains,
        "width": shape[0],
        "height": shape[1],
        "frames": frames,
        "created": datetime.fromtimestamp(
            max((root / f"g{gain}" / "meta.txt").stat().st_mtime for gain in gains),
            timezone.utc).isoformat(),
    }


def _load_columns(path, width, frames):
    values = np.fromfile(path, dtype=np.float32)
    if values.size != width * frames:
        raise ValueError(f"{path} 应包含 {width * frames} 个浮点数，实际 {values.size}")
    if not np.all(np.isfinite(values)):
        raise ValueError(f"{path} 包含非有限数值")
    return values.reshape(frames, width)


def _dark_analysis(columns, height):
    frames, width = columns.shape
    split = frames // 2
    if split < 2 or frames - split < 2:
        raise ValueError(f"暗场至少需要 4 帧，实际 {frames}")
    calibration = columns[:split]
    validation = columns[split:]
    profile = calibration.mean(axis=0)
    validation_profile = validation.mean(axis=0)
    black = float(profile.mean())
    offset = profile - black
    before = validation_profile - validation_profile.mean()
    after = before - offset
    before_std = float(np.std(before))
    after_std = float(np.std(after))
    reduction = 100.0 * (1.0 - after_std / before_std) if before_std else 0.0
    return {
        "width": width,
        "height": height,
        "black": black,
        "offset": offset,
        "pattern_before": before_std,
        "pattern_after": after_std,
        "reduction": reduction,
    }


def _moving_average(profile, columns):
    width = profile.size
    window = max(3, min(int(columns), width))
    if window % 2 == 0:
        window -= 1
    pad = window // 2
    padded = np.pad(profile, pad, mode="edge")
    return np.convolve(padded, np.ones(window) / window, mode="valid")


def _stripe_std(profile, columns):
    return float(np.std(profile - _moving_average(profile, columns)))


def _subtract_dark(columns, black, offset):
    frame_mean = columns.mean(axis=1)
    signal = np.maximum(0.0, frame_mean - black)
    alpha = OFFSET_HALF_SIGNAL / (OFFSET_HALF_SIGNAL + signal)
    return columns - black - alpha[:, None] * offset[None, :]


def _flat_analysis(columns, meta, dark):
    frames, width = columns.shape
    split = frames // 2
    if split < 2 or frames - split < 2:
        raise ValueError(f"平场至少需要 4 帧，实际 {frames}")
    calibration = _subtract_dark(columns[:split], dark["black"], dark["offset"])
    validation = _subtract_dark(columns[split:], dark["black"], dark["offset"])
    profile = calibration.mean(axis=0)
    validation_profile = validation.mean(axis=0)
    if float(profile.min()) <= 8.0:
        raise ValueError("平场亮度太低，请提高照明或曝光")
    saturation = float(meta.get("cam_saturated_fraction", "nan"))
    if not math.isfinite(saturation):
        saturation = 0.0
    if saturation > 0.01:
        raise ValueError(f"平场有 {saturation * 100:.2f}% 像素过曝，请降低曝光")
    if SMOOTH_COLUMNS >= width:
        raise ValueError("平场图像宽度不足以做局部基线")
    baseline = _moving_average(profile, SMOOTH_COLUMNS)
    if np.any(baseline <= 0):
        raise ValueError("平场局部基线无效")
    gain = baseline / profile
    low, high = MIN_GAIN_Q12 / 4096.0, MAX_GAIN_Q12 / 4096.0
    clamped = int(np.count_nonzero((gain < low) | (gain > high)))
    if clamped > max(2, width // 50):
        raise ValueError(f"平场有 {clamped} 列需要超出 {low:.2f}..{high:.2f} 的增益，"
                         "请检查照明均匀性")
    gain = np.clip(gain, low, high)
    gain /= gain.mean()
    gain = np.clip(gain, low, high)
    before = validation_profile
    after = validation_profile * gain
    before_std = _stripe_std(before, SMOOTH_COLUMNS)
    after_std = _stripe_std(after, SMOOTH_COLUMNS)
    reduction = 100.0 * (1.0 - after_std / before_std) if before_std else 0.0
    return {
        "gain": gain,
        "signal": float(profile.mean()),
        "pattern_before": before_std,
        "pattern_after": after_std,
        "reduction": reduction,
        "saturation": saturation,
    }


def _encode(width, height, buckets):
    data = bytearray(struct.pack(
        "<4sHHHHHHII", MAGIC, VERSION, HEADER_SIZE, width, height,
        len(buckets), FLAGS, HEADER_SIZE, 0))
    for gain, black, offset, flat_gain in buckets:
        offset_q8 = np.rint(offset * 256.0)
        if offset_q8.size and (offset_q8.min() < -32768 or offset_q8.max() > 32767):
            raise ValueError(f"增益 {gain} 的暗场偏移超出 Q8 int16")
        flat_q12 = np.rint(flat_gain * 4096.0)
        if flat_q12.size and (flat_q12.min() < MIN_GAIN_Q12 or
                              flat_q12.max() > MAX_GAIN_Q12):
            raise ValueError(f"增益 {gain} 的平场增益超出 Q12 范围")
        black_q8 = int(round(black * 256.0))
        if not 0 <= black_q8 <= 255 * 256:
            raise ValueError(f"增益 {gain} 的black level超出范围")
        data += struct.pack("<II", gain, black_q8)
        data += offset_q8.astype("<i2").tobytes()
        data += flat_q12.astype("<u2").tobytes()
    return bytes(data)


def _write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
                delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def compute(dark_root, flat_root, output):
    dark = _capture_directories(dark_root, "dark")
    flat = _capture_directories(flat_root, "flat")
    if set(dark) != set(flat):
        raise ValueError("暗场和平场的增益集合不一致")
    gains = sorted(dark)
    shape = None
    camera_results = [[], []]
    encoded = {}
    for gain in gains:
        dark_path, dark_meta = dark[gain]
        flat_path, flat_meta = flat[gain]
        current_shape = (_number(dark_meta, "width"), _number(dark_meta, "height"))
        if current_shape != (_number(flat_meta, "width"), _number(flat_meta, "height")):
            raise ValueError(f"增益 {gain} 的暗场和平场尺寸不一致")
        if shape is None:
            shape = current_shape
        if current_shape != shape:
            raise ValueError("不同增益的采集尺寸不一致")
        width, height = shape
        for cam in (0, 1):
            dark_frames = _number(dark_meta, "frames")
            flat_frames = _number(flat_meta, "frames")
            dark_columns = _load_columns(
                dark_path / f"cam{cam}_cols.f32", width, dark_frames)
            flat_columns = _load_columns(
                flat_path / f"cam{cam}_cols.f32", width, flat_frames)
            dark_result = _dark_analysis(dark_columns, height)
            flat_meta_camera = dict(flat_meta)
            flat_meta_camera["cam_saturated_fraction"] = flat_meta.get(
                f"cam{cam}_saturated_fraction", "0")
            flat_result = _flat_analysis(flat_columns, flat_meta_camera, dark_result)
            camera_results[cam].append({
                "gain": gain,
                "black_lsb": dark_result["black"],
                "dark_reduction": dark_result["reduction"],
                "flat_signal_lsb": flat_result["signal"],
                "flat_reduction": flat_result["reduction"],
                "flat_saturation": flat_result["saturation"],
            })
            if cam not in encoded:
                encoded[cam] = []
            encoded[cam].append((
                gain, dark_result["black"], dark_result["offset"], flat_result["gain"]))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    files = {}
    for cam in (0, 1):
        data = _encode(shape[0], shape[1], encoded[cam])
        path = output / f"cam{cam}.fpn"
        _write_atomic(path, data)
        files[f"cam{cam}.fpn"] = hashlib.sha256(data).hexdigest()
    result = {
        "version": VERSION,
        "flags": FLAGS,
        "width": shape[0],
        "height": shape[1],
        "gains": gains,
        "created": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "cameras": [
            {"camera": cam, "buckets": camera_results[cam]}
            for cam in (0, 1)
        ],
    }
    _write_atomic(output / "result.json",
                  json.dumps(result, ensure_ascii=False, indent=2).encode())
    return result
