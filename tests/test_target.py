import asyncio
from types import SimpleNamespace
from xml.etree import ElementTree

import cv2
import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

from tide_configurator.app import create_app
from tide_configurator.jobs import Jobs
from tide_configurator.target import configuration, specification, svg


def raster():
    """Rasterize our integer SVG rectangles independently of browser rendering."""
    spec = specification()
    image = np.full((spec['view_height'], spec['view_width']), 255, np.uint8)
    for element in ElementTree.fromstring(svg()).iter():
        if element.tag.endswith('rect') and element.get('x') is not None:
            x, y, width, height = (round(float(element.get(k))) for k in ('x', 'y', 'width', 'height'))
            image[y:y+height, x:x+width] = 255 if element.get('fill') == 'white' else 0
    return image


def test_screen_svg_has_kalibr_tags_orientation_and_measurable_outer_squares():
    image = raster()
    parameters = cv2.aruco.DetectorParameters()
    parameters.markerBorderBits = 2
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), parameters)
    corners, identifiers, _ = detector.detectMarkers(image)
    assert sorted(identifiers.ravel().tolist()) == list(range(24))
    # Kalibr's coordinates are bottom-left, ID increments horizontally then up.
    for identifier, polygon in zip(identifiers.ravel(), corners):
        center = polygon.reshape(4, 2).mean(axis=0)
        assert np.allclose(center, [120 + identifier % 6 * 130, 120 + (3 - identifier // 6) * 130], atol=1)
        # Kalibr's generator uses rotation=2, so the canonical marker's first
        # OpenCV corner is at the bottom right of the displayed tag.
        assert np.all(polygon.reshape(4, 2)[0] > center)
    spec = specification()
    assert spec['span_start'] == 40 and spec['span_end'] == 850
    assert np.all(image[40:70, 40:70] == 0)
    assert np.all(image[40:70, 820:850] == 0)
    assert configuration(259.2)['tagSize'] == pytest.approx(.032)
    assert configuration(518.4)['tagSize'] == pytest.approx(.064)


@pytest.mark.parametrize('width', [None, 0, -1, float('inf'), float('nan'), True, 10001])
def test_invalid_physical_scale_is_rejected(width):
    with pytest.raises(ValueError):
        configuration(width)


def test_calibration_recording_requires_and_persists_measured_scale(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        state = client.app.state.console
        state.device.info = dict(connected=True, host='device')
        calls = []
        def start(*args):
            calls.append(args)
            return '/recording', dict(state='preparing', recording_id='d'*32)
        state.device.start = start
        assert client.post('/api/recordings', json=dict(kind='calibration')).status_code == 400
        assert not calls
        response = client.post('/api/recordings', json=dict(kind='calibration', board_width_mm=259.2))
        assert response.status_code == 200
        assert response.json()['board_width_mm'] == 259.2
        state.active = None
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/state').json()['captures'][0]['board_width_mm'] == 259.2


def test_job_uses_measured_target_and_does_not_reuse_previous_scale(tmp_path, monkeypatch):
    commands = []
    async def finished():
        return 1
    async def spawn(*command, **kwargs):
        commands.append(command)
        return SimpleNamespace(wait=finished)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    noise = yaml.safe_dump(dict(update_rate=200, accelerometer_noise_density=.01,
                                accelerometer_random_walk=.001, gyroscope_noise_density=.001,
                                gyroscope_random_walk=.0001))
    async def run():
        jobs = Jobs(tmp_path)
        for width in (259.2, 518.4):
            job = await jobs.start(tmp_path / 'source', 'fisheye', noise, width)
            folder = jobs.folder(job['id'])
            command = commands[-1]
            path = command[command.index('--target') + 1]
            assert path == str(folder / 'target.yaml')
            target = yaml.safe_load((folder / 'target.yaml').read_text())
            assert target == configuration(width)
            assert job['target'] == target and job['board_width_mm'] == width
            await asyncio.gather(*jobs.tasks)
        assert commands[0] != commands[1]
        assert yaml.safe_load((jobs.directory / jobs.all()[-1]['id'] / 'target.yaml').read_text())['tagSize'] == pytest.approx(.032)
    asyncio.run(run())

