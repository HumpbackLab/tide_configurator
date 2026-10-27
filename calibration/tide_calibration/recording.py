"""Read and validate TideSense recordings and serialize one raw-time ROS1 bag."""
import csv
import math
import struct

import yaml

MAGIC = b'TIDESENSE_RAW_V1\n'
HEADER = struct.Struct('<IIIIQQQIIII')
RESOLUTIONS = {(640, 400), (640, 480), (1280, 800)}
MAX_RECORD = HEADER.size + 2 * 1280 * 800


def image_bytes(header):
    if (header[0], header[1], header[9], header[10]) != (0x54444341, 1, 1, 0):
        raise ValueError('Invalid camera header or FSIN disabled')
    if (header[2], header[3]) not in RESOLUTIONS:
        raise ValueError('Unsupported capture resolution')
    return header[2] * header[3]


def frames(source, *, hasher=None, progress=None):
    with (source / 'frames.bin').open('rb') as stream:
        f = _HashReader(stream, hasher, progress)
        magic = f.read(len(MAGIC))
        compressed = magic == b'TIDESENSE_ZST_V1\n'
        if magic != MAGIC and not compressed:
            raise ValueError('Invalid archive magic')
        if compressed:
            import zstandard
            decompressor = zstandard.ZstdDecompressor()
        frame_index = 0
        while True:
            offset = f.tell()
            if compressed:
                length = f.read(4)
                if not length:
                    return
                if len(length) != 4:
                    raise ValueError('Truncated record length')
                size = struct.unpack('<I', length)[0]
                if not 0 < size <= MAX_RECORD + MAX_RECORD // 128 + 4096:
                    raise ValueError('Invalid compressed record size')
                data = f.read(size)
                if len(data) != size:
                    raise ValueError('Truncated compressed record')
                try:
                    content_size = zstandard.frame_content_size(data)
                    if content_size not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR) and content_size > MAX_RECORD:
                        raise ValueError('Oversized decompressed frame')
                    data = decompressor.decompress(data, max_output_size=MAX_RECORD)
                except zstandard.ZstdError as exc:
                    raise ValueError(f'Frame #{frame_index + 1} decompression/checksum failed at byte {offset}: {exc}') from exc
            else:
                header_data = f.read(HEADER.size)
                if not header_data:
                    return
                if len(header_data) != HEADER.size:
                    raise ValueError('Truncated frame header')
                count = image_bytes(HEADER.unpack(header_data))
                data = header_data + f.read(2 * count)
            if len(data) < HEADER.size:
                raise ValueError('Truncated/invalid frame record; refuse export')
            h = HEADER.unpack_from(data)
            count = image_bytes(h)
            if len(data) != HEADER.size + 2 * count:
                raise ValueError('Truncated/invalid frame record; refuse export')
            pixels = data[HEADER.size:]
            if compressed:
                import numpy as np
                residual = np.frombuffer(pixels, dtype=np.uint8).reshape(2 * h[3], h[2])
                pixels = np.cumsum(residual, axis=1, dtype=np.uint8).tobytes()
            yield h, pixels[:count], pixels[count:]
            frame_index += 1


class _HashReader:
    def __init__(self, stream, hasher, progress):
        self.stream, self.hasher, self.progress = stream, hasher, progress

    def read(self, size):
        data = self.stream.read(size)
        if self.hasher is not None:
            self.hasher.update(data)
        if self.progress:
            self.progress(self.stream.tell())
        return data

    def tell(self):
        return self.stream.tell()


def imu_rows(source, *, hasher=None):
    with (source / 'imu.csv').open('rb') as f:
        def lines():
            for line in f:
                if hasher is not None:
                    hasher.update(line)
                if not line.startswith(b'#'):
                    yield line.decode('utf-8')
        for row in csv.reader(lines()):
            if len(row) not in (7, 10):
                raise ValueError('IMU CSV requires timestamp + 6 SI values (or legacy 9-value rows)')
            t, values = int(row[0]), [float(v) for v in row[1:]]
            if t <= 0 or not all(math.isfinite(v) for v in values):
                raise ValueError('Invalid IMU timestamp/value')
            yield t, values if len(values)==6 else values[:3]+values[6:]


def metadata(source):
    if not (source / 'COMPLETE').is_file() or (source / 'INCOMPLETE').exists():
        raise ValueError('Dataset is incomplete; refuse to present it as a valid dataset')
    data = (source / 'metadata.yaml').read_bytes()
    meta = yaml.safe_load(data)
    if not isinstance(meta, dict) or meta.get('schema') not in ('tidesense-dataset-v1', 'tidesense-dataset-v2') or not meta.get('complete'):
        raise ValueError('Invalid metadata schema/completion')
    return meta, data


class Validator:
    def __init__(self, meta):
        self.meta = meta
        self.first = self.last = self.previous = self.resolution = None
        self.imu_first = self.imu_last = None
        self.count = self.samples = 0
        self.gaps = {}
        recovery = meta.get('recovery')
        if recovery:
            if not isinstance(recovery, dict):
                raise ValueError('Invalid recovery provenance')
            for gap in recovery.get('camera_gaps', []):
                after, count = gap.get('after_pair'), gap.get('count')
                if (type(after) is not int or type(count) is not int or not 0 <= after <= meta['pairs']
                        or count < 1 or after in self.gaps):
                    raise ValueError('Invalid declared camera recovery gap')
                self.gaps[after] = count
            if recovery.get('source_pairs') != meta['pairs']+sum(self.gaps.values()):
                raise ValueError('Recovery counts do not match camera gaps')

    def camera(self, h):
        _, _, width, height, seq, left, right, lr, rr, _, _ = h
        image_bytes(h)
        if self.resolution is None:
            self.resolution = [width, height]
        if self.resolution != [width, height]:
            raise ValueError('Resolution changed within recording')
        if not left or not right or abs(right-left) > 20000000:
            raise ValueError('Invalid camera timestamp/skew')
        p = self.previous
        step = 1+self.gaps.get(self.count, 0)
        if p and (seq != p[4]+step or not 0 < left-p[5] <= 75000000*step
                  or not 0 < right-p[6] <= 75000000*step
                  or (lr-p[7]) % 2**32 != step or (rr-p[8]) % 2**32 != step):
            raise ValueError('Camera gap/nonmonotonic sequence')
        self.previous = h
        if self.first is None:
            self.first = left
        self.last = left
        self.count += 1

    def imu(self, t):
        if self.imu_last is not None and not 0 < t-self.imu_last <= 7500000:
            raise ValueError('IMU gap/nonmonotonic timestamp')
        if self.imu_first is None:
            self.imu_first = t
        self.imu_last = t
        self.samples += 1

    def finish(self):
        if self.count < 2 or self.samples < 2 or not self.imu_first < self.first <= self.last < self.imu_last:
            raise ValueError('Insufficient frames/IMU or IMU does not bracket image time range')
        if self.count != self.meta['pairs'] or self.samples != self.meta['imu_samples']:
            raise ValueError('Metadata counts differ from archive (truncation/corruption)')
        if 'width' in self.meta and [self.meta['width'], self.meta.get('height')] != self.resolution:
            raise ValueError('Metadata dimensions differ from archive')
        report = dict(pairs=self.count, imu_samples=self.samples, resolution=self.resolution,
                      camera_hz=(self.count-1)*1e9/(self.last-self.first),
                      imu_hz=(self.samples-1)*1e9/(self.imu_last-self.imu_first), camera_offset_ns=0)
        if self.gaps:
            report.update(dropped_camera_pairs=sum(self.gaps.values()),
                          source_camera_pairs=self.meta['recovery']['source_pairs'])
        return report


def validate(source):
    check = Validator(metadata(source)[0])
    for h, _, _ in frames(source):
        check.camera(h)
    for t, _ in imu_rows(source):
        check.imu(t)
    return check.finish()


def write_bag(source, destination, *, validator=None, hashes=None, progress=None):
    import numpy as np
    from rosbags.rosbag1 import Writer
    from rosbags.typesys import Stores, get_typestore
    store = get_typestore(Stores.ROS1_NOETIC)
    types = store.types
    Time = types['builtin_interfaces/msg/Time']
    Header = types['std_msgs/msg/Header']
    Image = types['sensor_msgs/msg/Image']
    Imu = types['sensor_msgs/msg/Imu']
    Vector = types['geometry_msgs/msg/Vector3']
    Quaternion = types['geometry_msgs/msg/Quaternion']
    def header(t, frame):
        return Header(seq=0, stamp=Time(sec=t//10**9, nanosec=t % 10**9), frame_id=frame)
    with Writer(destination) as writer:
        cams = [writer.add_connection(f'/cam{i}/image_raw', Image.__msgtype__, typestore=store) for i in range(2)]
        inertial = writer.add_connection('/imu0', Imu.__msgtype__, typestore=store)
        def write_imu(t, v):
            covariance = np.zeros(9, dtype=np.float64)
            orientation_covariance = covariance.copy()
            orientation_covariance[0] = -1.0
            message = Imu(header(t, 'imu0'), Quaternion(0., 0., 0., 1.), orientation_covariance,
                          Vector(*v[:3]), covariance, Vector(*v[3:6]), covariance)
            writer.write(inertial, t, store.serialize_ros1(message, Imu.__msgtype__))
        def checked_imu():
            for t, v in imu_rows(source, hasher=hashes['imu.csv'] if hashes else None):
                if validator:
                    validator.imu(t)
                yield t, v
        samples = iter(checked_imu())
        current = next(samples, None)
        processed = 0
        def frame_progress(value):
            nonlocal processed
            processed = value
        for h, left, right in frames(source, hasher=hashes['frames.bin'] if hashes else None,
                                     progress=frame_progress):
            if validator:
                validator.camera(h)
            t = h[5]
            while current and current[0] <= t:
                write_imu(*current)
                current = next(samples, None)
            for i, pixels in enumerate((left, right)):
                msg = Image(header(t, f'cam{i}'), h[3], h[2], 'mono8', 0, h[2], np.frombuffer(pixels, dtype=np.uint8))
                writer.write(cams[i], t, store.serialize_ros1(msg, Image.__msgtype__))
            if progress:
                progress(processed, validator.count if validator else 0)
        while current:
            write_imu(*current)
            current = next(samples, None)

    return validator.finish() if validator else None
