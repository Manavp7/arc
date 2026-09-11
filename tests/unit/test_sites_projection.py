"""Camera image dimensions must change geometry scale without moving a normalized point."""

import math

import pytest
from sio_fusion.projection import CameraCalibration, GroundProjector, haversine_m

from sio_schemas import BBox


def row(**config):
    return {
        "source_id": "camera-a",
        "lat": 37.77,
        "lon": -122.41,
        "zone_id": "zone-a",
        "config": {
            "bearing_deg": 30,
            "fov_deg": 70,
            "vfov_deg": 45,
            "height_m": 6,
            "tilt_deg": 18,
            "range_m": 60,
            **config,
        },
    }


def normalized_box(width, height):
    return BBox(x1=0.6 * width, y1=0.3 * height, x2=0.7 * width, y2=0.55 * height)


@pytest.mark.parametrize("width,height", [(1920, 1080), (640, 480), (3840, 2160)])
def test_configured_frame_dimensions_preserve_normalized_ground_fix(width, height):
    default = CameraCalibration.from_source_row(row())
    configured = CameraCalibration.from_source_row(row(frame_width=width, frame_height=height))
    assert default is not None and configured is not None
    assert (configured.frame_width, configured.frame_height) == (width, height)
    default_fix = GroundProjector(default).project(normalized_box(1280, 720))
    changed_fix = GroundProjector(configured).project(normalized_box(width, height))
    assert default_fix is not None and changed_fix is not None
    assert changed_fix.range_m == default_fix.range_m
    assert changed_fix.bearing_deg == default_fix.bearing_deg
    assert haversine_m(changed_fix.geo, default_fix.geo) < 0.00001
    assert math.isfinite(changed_fix.position_sigma_m)
    assert changed_fix.position_sigma_m > 0


def test_projection_uncertainty_reflects_frame_resolution():
    low = CameraCalibration.from_source_row(row(frame_width=320, frame_height=180))
    high = CameraCalibration.from_source_row(row(frame_width=1920, frame_height=1080))
    assert low is not None and high is not None
    low_fix = GroundProjector(low).project(BBox(x1=160, x2=180, y1=0, y2=45))
    high_fix = GroundProjector(high).project(BBox(x1=960, x2=1080, y1=0, y2=270))
    assert low_fix is not None and high_fix is not None
    assert high_fix.range_m == low_fix.range_m
    assert high_fix.position_sigma_m < low_fix.position_sigma_m


@pytest.mark.parametrize(
    "config",
    [
        {"frame_width": 0},
        {"frame_height": 0},
        {"frame_width": -20},
        {"frame_height": "invalid"},
        {"frame_width": float("inf")},
        {"height_m": 0},
        {"range_m": -1},
        {"fov_deg": 180},
        {"vfov_deg": 0},
        {"bearing_deg": float("nan")},
        {"tilt_deg": float("inf")},
    ],
)
def test_invalid_stored_camera_config_is_unusable_instead_of_dividing_by_zero(config):
    assert CameraCalibration.from_source_row(row(**config)) is None


def test_unpositioned_source_remains_unprojectable():
    source = row(frame_width=1920, frame_height=1080)
    source["lat"] = None
    assert CameraCalibration.from_source_row(source) is None
