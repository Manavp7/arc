"""Tests for sensor fusion (PRD M5).

The acceptance criterion is "N observations of one object collapse to one entity with fused state and
provenance", so that is what most of these assert — along with the failure that matters more:
observations of *different* objects must NOT collapse. A fusion service that merges everything scores
perfectly on the first test and is worse than useless.

Nothing here uses the simulator's ``agent_id``. Association is tested the way the real system has to
do it: position, time, class and appearance.
"""

from __future__ import annotations

import math
from datetime import timedelta

import pytest
from sio_fusion.fuse import (
    CLASS_TO_ENTITY,
    Observation2D,
    PositionFilter,
    SensorFusion,
    entity_type_for,
    observation_from_gps,
    observation_from_rfid,
    types_compatible,
)
from sio_fusion.projection import (
    CameraCalibration,
    GroundProjector,
    from_local_metres,
    haversine_m,
    to_local_metres,
)

from sio_schemas import BBox, EntityType, Geo, Modality, utc_now

ORIGIN = Geo(lat=37.7749, lon=-122.4194)


def gps_observation(
    east: float,
    north: float,
    *,
    device: str = "gps-truck-1",
    label: str = "truck",
    seconds: float = 0.0,
) -> Observation2D:
    return Observation2D(
        source_id=device,
        modality=Modality.GPS,
        ts=utc_now() + timedelta(seconds=seconds),
        east=east,
        north=north,
        sigma_m=2.5,
        label=label,
        # Namespaced exactly as observation_from_gps does it. A helper producing un-namespaced ids
        # would exercise something the system never emits — and did: the device-conflict test passed
        # for the wrong reason, because the positional gate happened to separate the trucks anyway.
        device_id=f"gps:{device}",
    )


def camera_observation(
    east: float,
    north: float,
    *,
    source: str = "cam-gate-a",
    label: str = "truck",
    seconds: float = 0.0,
    sigma: float = 5.0,
    track: str | None = None,
    embedding: tuple[float, ...] | None = None,
) -> Observation2D:
    return Observation2D(
        source_id=source,
        modality=Modality.VIDEO,
        ts=utc_now() + timedelta(seconds=seconds),
        east=east,
        north=north,
        sigma_m=sigma,
        label=label,
        track_id=track,
        embedding=embedding,
    )


# ------------------------------------------------------------------- coordinates
def test_local_metres_round_trip() -> None:
    """The filter runs in metres; a projection error would poison every gate."""
    for east, north in ((0, 0), (120.5, -80.25), (400, 260)):
        geo = from_local_metres(east, north, ORIGIN)
        back_east, back_north = to_local_metres(geo, ORIGIN)
        assert back_east == pytest.approx(east, abs=0.01)
        assert back_north == pytest.approx(north, abs=0.01)


def test_local_metres_agree_with_haversine() -> None:
    geo = from_local_metres(100.0, 0.0, ORIGIN)
    assert haversine_m(ORIGIN, geo) == pytest.approx(100.0, abs=0.5)


# --------------------------------------------------------------------- projection
def calibration(**kwargs: object) -> CameraCalibration:
    defaults: dict[str, object] = {
        "source_id": "cam-test",
        "geo": ORIGIN,
        "bearing_deg": 0.0,
        "fov_deg": 70.0,
        "range_m": 60.0,
        "height_m": 6.0,
    }
    defaults.update(kwargs)
    return CameraCalibration(**defaults)  # type: ignore[arg-type]


def test_projection_puts_a_centred_box_on_the_optical_axis() -> None:
    projector = GroundProjector(calibration(bearing_deg=90.0))
    # Centred horizontally, low in the frame: straight ahead and close.
    fix = projector.project(BBox(x1=590, y1=500, x2=690, y2=660))
    assert fix is not None
    assert fix.bearing_deg == pytest.approx(90.0, abs=1.0)
    assert 0 < fix.range_m <= 60


def test_projection_maps_horizontal_position_to_bearing() -> None:
    projector = GroundProjector(calibration(bearing_deg=0.0, fov_deg=70.0))
    left = projector.project(BBox(x1=0, y1=500, x2=100, y2=660))
    right = projector.project(BBox(x1=1180, y1=500, x2=1280, y2=660))
    assert left is not None and right is not None
    # Left of frame is anticlockwise of the axis, right is clockwise.
    assert left.bearing_deg > 300 or left.bearing_deg < 0 + 1
    assert 0 < right.bearing_deg < 40


def test_projection_maps_vertical_position_to_range() -> None:
    """Lower in the frame means closer. Getting this backwards would invert the whole site."""
    projector = GroundProjector(calibration())
    near = projector.project(BBox(x1=600, y1=600, x2=700, y2=700))
    far = projector.project(BBox(x1=600, y1=300, x2=700, y2=330))
    assert near is not None and far is not None
    assert near.range_m < far.range_m


def test_projection_refuses_boxes_above_the_horizon() -> None:
    """No ground intersection exists, so there is no honest answer."""
    projector = GroundProjector(calibration())
    assert projector.project(BBox(x1=600, y1=10, x2=700, y2=120)) is None


def test_projection_uncertainty_grows_with_range() -> None:
    """A distant fix must be weighted less, or a 50 m detection drags an entity off its GPS track."""
    projector = GroundProjector(calibration())
    # With the physical model, row 700 is ~7 m away and row 170 is ~55 m: a real spread rather than
    # two boxes that both land inside a few metres.
    near = projector.project(BBox(x1=600, y1=620, x2=700, y2=700))
    far = projector.project(BBox(x1=600, y1=150, x2=700, y2=170))
    assert near is not None and far is not None
    assert far.range_m > near.range_m * 3, (
        "the two boxes must differ in range for this to mean anything"
    )
    assert far.position_sigma_m > near.position_sigma_m
    assert near.confidence > far.confidence


def test_camera_visibility_check() -> None:
    projector = GroundProjector(calibration(bearing_deg=0.0, fov_deg=60.0, range_m=50.0))
    assert projector.sees(from_local_metres(0, 30, ORIGIN)), "straight ahead, in range"
    assert not projector.sees(from_local_metres(0, -30, ORIGIN)), "behind the camera"
    assert not projector.sees(from_local_metres(0, 200, ORIGIN)), "beyond its range"


# ------------------------------------------------------------------------ filter
def test_filter_converges_on_a_stationary_object() -> None:
    position_filter = PositionFilter(10.0, 20.0, sigma_m=5.0)
    for _ in range(12):
        position_filter.predict(0.5)
        position_filter.update(10.0, 20.0, 2.5)
    east, north = position_filter.position
    assert (east, north) == pytest.approx((10.0, 20.0), abs=0.5)
    assert math.hypot(*position_filter.velocity) < 0.5
    assert position_filter.position_sigma_m < 2.5, "repeated fixes must reduce uncertainty"


def test_filter_estimates_velocity() -> None:
    position_filter = PositionFilter(0.0, 0.0, sigma_m=2.0)
    for step in range(1, 16):
        position_filter.predict(1.0)
        position_filter.update(step * 5.0, 0.0, 2.0)
    v_east, v_north = position_filter.velocity
    assert v_east == pytest.approx(5.0, abs=1.5), "should learn 5 m/s eastward"
    assert abs(v_north) < 1.0


def test_mahalanobis_accounts_for_track_uncertainty() -> None:
    """The reason for gating on sigma rather than metres.

    A fresh, uncertain track should accept a fix that a well-established one rejects — one fixed
    radius cannot serve both a stationary forklift and a 20 m/s drone.
    """
    fresh = PositionFilter(0.0, 0.0, sigma_m=10.0)
    settled = PositionFilter(0.0, 0.0, sigma_m=10.0)
    for _ in range(20):
        settled.predict(0.5)
        settled.update(0.0, 0.0, 1.0)

    offset = (12.0, 0.0)
    assert fresh.mahalanobis(*offset, 2.5) < settled.mahalanobis(*offset, 2.5)


# ------------------------------------------------------------------ class mapping
def test_detector_classes_map_to_entity_types() -> None:
    assert entity_type_for("bus") is EntityType.TRUCK, "a bus body stands in for a box truck here"
    assert entity_type_for("person") is EntityType.PERSON
    assert entity_type_for("airplane") is EntityType.DRONE, "what COCO calls a quadcopter"
    assert entity_type_for("wardrobe") is EntityType.UNKNOWN


def test_type_compatibility_allows_vehicle_confusion_but_not_nonsense() -> None:
    assert types_compatible(EntityType.TRUCK, EntityType.FORKLIFT), (
        "a camera cannot tell these apart"
    )
    assert types_compatible(EntityType.TRUCK, EntityType.UNKNOWN), "unknown must not block a match"
    assert not types_compatible(EntityType.PERSON, EntityType.TRUCK)
    assert not types_compatible(EntityType.DRONE, EntityType.PERSON)


def test_every_mapped_class_has_a_compatibility_group() -> None:
    """A class that maps to a type with no group can never fuse with anything."""
    from sio_fusion.fuse import COMPATIBLE

    grouped = set().union(*COMPATIBLE)
    for label, entity_type in CLASS_TO_ENTITY.items():
        if entity_type in (EntityType.SHIP, EntityType.UNKNOWN):
            continue  # deliberately ungrouped: nothing else should fuse with them
        assert entity_type in grouped, f"{label} -> {entity_type} is in no compatibility group"


# ---------------------------------------------------------- the acceptance test
def test_three_sensors_on_one_object_collapse_to_one_entity() -> None:
    """PRD M5's acceptance criterion, exactly.

    A camera, a GPS tracker and an RFID reader all observe the same truck at the same place. They must
    produce ONE entity carrying provenance from all three — and crucially, association here is by
    position, time and class, never by the simulator's identity fields.
    """
    fusion = SensorFusion(ORIGIN, assoc_radius_m=25.0, time_window_s=5.0)

    fusion.observe(gps_observation(100.0, 50.0, device="gps-truck-1"))
    fusion.observe(camera_observation(102.0, 51.5, source="cam-gate-a", track="trk-cam-gate-a-1"))
    fusion.observe(
        observation_from_rfid(
            {"tag_id": "TAG-ABC-123", "plate": "ABC-123", "zone_id": "gate_a"},
            "iot-rfid-gate-a",
            utc_now(),
            from_local_metres(101.0, 50.5, ORIGIN),
            ORIGIN,
        )
    )

    assert len(fusion.entities) == 1, (
        f"three observations of one truck produced {len(fusion.entities)} entities"
    )
    entity = next(iter(fusion.entities.values()))
    assert entity.observations == 3
    assert entity.modalities == {"gps", "video", "rfid"}
    assert entity.is_multi_sensor
    assert {entry.source_id for entry in entity.provenance} == {
        "gps-truck-1",
        "cam-gate-a",
        "iot-rfid-gate-a",
    }
    # The fused position sits among the three inputs, weighted toward the most precise.
    east, north = entity.filter.position
    assert 99 < east < 103
    assert 49 < north < 53
    assert fusion.stats["matched_by_position"] >= 1, "the camera must have matched on position"


def test_two_objects_far_apart_do_not_collapse() -> None:
    """The failure that matters more. A fusion service that merges everything passes the test above."""
    fusion = SensorFusion(ORIGIN, assoc_radius_m=25.0)
    fusion.observe(gps_observation(0.0, 0.0, device="gps-truck-1"))
    fusion.observe(gps_observation(200.0, 150.0, device="gps-truck-2"))
    assert len(fusion.entities) == 2


def test_a_person_and_a_truck_at_the_same_spot_stay_separate() -> None:
    """A worker standing beside their truck is two entities, however close they are."""
    fusion = SensorFusion(ORIGIN, assoc_radius_m=25.0)
    fusion.observe(camera_observation(50.0, 50.0, label="truck", track="t1"))
    fusion.observe(camera_observation(50.5, 50.5, label="person", track="t2"))
    assert len(fusion.entities) == 2
    assert fusion.stats["rejected_by_class"] >= 1


def test_a_device_id_is_identity_even_after_a_large_jump() -> None:
    """A GPS tracker that has moved 500 m is still the same tracker.

    Gating a device id on position would split one vehicle into many every time a fix was delayed.
    """
    fusion = SensorFusion(ORIGIN, assoc_radius_m=25.0)
    fusion.observe(gps_observation(0.0, 0.0, device="gps-truck-1"))
    fusion.observe(gps_observation(500.0, 400.0, device="gps-truck-1", seconds=30))
    assert len(fusion.entities) == 1
    assert fusion.stats["matched_by_device"] == 1


def test_a_camera_track_stays_bound_once_associated() -> None:
    """Re-solving association every frame lets one bad frame reassign a truck to its neighbour."""
    fusion = SensorFusion(ORIGIN)
    fusion.observe(camera_observation(10.0, 10.0, track="trk-1"))
    for step in range(5):
        fusion.observe(camera_observation(10.0 + step, 10.0, track="trk-1", seconds=step))
    assert len(fusion.entities) == 1
    assert fusion.stats["matched_by_track"] >= 4


def test_observations_outside_the_time_window_do_not_associate() -> None:
    fusion = SensorFusion(ORIGIN, time_window_s=5.0)
    fusion.observe(camera_observation(10.0, 10.0, track="a"))
    fusion.observe(camera_observation(10.0, 10.0, track="b", seconds=600))
    assert len(fusion.entities) == 2, "ten minutes apart is a different visit, not the same object"


def test_appearance_only_breaks_ties_position_could_not() -> None:
    """Appearance is the last resort: two identical white vans look the same."""
    vector = tuple([1.0] + [0.0] * 31)
    fusion = SensorFusion(ORIGIN, assoc_radius_m=10.0, reid_threshold=0.9)
    fusion.observe(camera_observation(0.0, 0.0, track="t1", embedding=vector))
    # Far outside the positional gate, but visually identical.
    fusion.observe(camera_observation(90.0, 90.0, track="t2", embedding=vector, seconds=1))
    assert len(fusion.entities) == 1
    assert fusion.stats["matched_by_appearance"] == 1


def test_entities_expire_when_nothing_is_seen() -> None:
    fusion = SensorFusion(ORIGIN, max_stale_s=0.0)
    fusion.observe(gps_observation(0.0, 0.0))
    fusion.observe(gps_observation(50.0, 50.0, device="gps-other"))
    assert fusion.stats["expired"] >= 1


def test_single_observations_are_not_published() -> None:
    """A sighting is not an object. Publishing one fills the world model with plausible ghosts."""
    fusion = SensorFusion(ORIGIN, min_observations=2)
    fusion.observe(camera_observation(10.0, 10.0, track="t1"))
    assert list(fusion.publishable()) == []
    fusion.observe(camera_observation(11.0, 10.0, track="t1", seconds=1))
    assert len(list(fusion.publishable())) == 1


def test_ground_truth_identity_never_reaches_the_entity() -> None:
    """The simulator ships agent_id and label in its payloads; using either would be cheating."""
    fusion = SensorFusion(ORIGIN)
    observation = gps_observation(5.0, 5.0)
    observation.attributes.update(
        {"agent_id": "truck-0001", "label": "Truck XPY-699", "state": "docked"}
    )
    fusion.observe(observation)
    fusion.observe(gps_observation(5.5, 5.0, seconds=1))
    entity = next(iter(fusion.entities.values()))
    assert "agent_id" not in entity.attributes
    assert "label" not in entity.attributes
    assert entity.attributes["state"] == "docked", "non-identity attributes are still useful"


def test_gps_type_beats_a_camera_guess() -> None:
    """A tracker declares its own kind; a camera can only tell a vehicle from a person."""
    fusion = SensorFusion(ORIGIN)
    fusion.observe(camera_observation(0.0, 0.0, label="truck", track="t1"))
    fusion.observe(gps_observation(1.0, 0.5, label="forklift", device="gps-fork-1", seconds=1))
    entity = next(iter(fusion.entities.values()))
    assert entity.entity_type is EntityType.FORKLIFT


def test_entity_state_carries_position_velocity_and_uncertainty() -> None:
    fusion = SensorFusion(ORIGIN)
    for step in range(8):
        fusion.observe(gps_observation(step * 4.0, 0.0, seconds=step))
    entity = next(iter(fusion.entities.values()))
    state = fusion.to_entity_state(entity)

    assert state.geo is not None
    assert state.velocity is not None and state.velocity.speed_mps > 1.0
    assert state.heading_deg is not None and 60 < state.heading_deg < 120, "moving east"
    assert state.covariance is not None and len(state.covariance) == 4
    assert 0 < state.confidence <= 0.99


def test_provenance_is_bounded() -> None:
    """Provenance is evidence for an explanation, not an audit log; the audit table holds that."""
    fusion = SensorFusion(ORIGIN)
    for step in range(120):
        fusion.observe(gps_observation(step * 0.1, 0.0, seconds=step * 0.1))
    entity = next(iter(fusion.entities.values()))
    assert len(entity.provenance) <= 40
    assert entity.observations == 120, "the count is still complete"


def test_gps_observation_uses_the_device_as_identity_not_the_agent() -> None:
    observation = observation_from_gps(
        {"entity_type": "truck", "agent_id": "truck-0007", "hdop_m": 2.0, "plate": "XYZ-1"},
        "gps-truck-0007",
        utc_now(),
        ORIGIN,
        ORIGIN,
    )
    assert observation.device_id == "gps:gps-truck-0007", (
        "the device id is legitimate identity, namespaced by kind"
    )
    assert "agent_id" not in observation.attributes, "ground-truth identity must not be carried"
    assert observation.attributes["plate"] == "XYZ-1", "a plate is evidence, and is allowed"


def test_describe_reports_association_reasons() -> None:
    """An operator asking 'why is that one entity?' needs the breakdown."""
    fusion = SensorFusion(ORIGIN)
    fusion.observe(gps_observation(0.0, 0.0))
    fusion.observe(camera_observation(1.0, 1.0, track="t1"))
    description = fusion.describe()
    assert set(description["stats"]) >= {
        "matched_by_device",
        "matched_by_position",
        "matched_by_appearance",
        "rejected_by_gate",
        "rejected_by_class",
    }
    assert description["entities"] >= 1


# --------------------------------------------------- device exclusivity (regression)
def test_two_trackers_close_together_stay_separate() -> None:
    """A device id is identity, and therefore also non-identity.

    Observed live: six trucks queued 16 m apart at the gate all fell inside the 25 m association
    radius, so five separate GPS trackers were absorbed into a single entity that then claimed 39,651
    observations and five devices. The positional gate cannot fix this — the trucks really are that
    close — but their device ids prove they are different objects.
    """
    fusion = SensorFusion(ORIGIN, assoc_radius_m=25.0)
    for index in range(6):
        # Deliberately 6 m apart: close enough that the positional gate *would* merge them, so only
        # the device rule can keep them separate. At 16 m the gate separates them on its own and the
        # test would pass while proving nothing — which is exactly what it did at first.
        fusion.observe(gps_observation(index * 6.0, 0.0, device=f"gps-truck-{index}"))

    assert len(fusion.entities) == 6, (
        f"six trackers must be six entities, got {len(fusion.entities)}"
    )
    assert fusion.stats["rejected_by_device_conflict"] > 0, (
        "the trucks must have been separated by device identity, not merely by distance"
    )
    for entity in fusion.entities.values():
        assert len(entity.device_ids) == 1, "an entity must never accumulate two tracker ids"


def test_two_devices_of_different_kinds_may_share_an_entity() -> None:
    """A truck carries a GPS tracker *and* an RFID tag, so conflict must be per device kind.

    A global "a different id means a different object" rule refused exactly the multi-sensor merge
    that fusion exists to perform, and broke the M5 acceptance test.
    """
    fusion = SensorFusion(ORIGIN, assoc_radius_m=25.0)
    fusion.observe(gps_observation(10.0, 10.0, device="gps-truck-1"))
    fusion.observe(
        observation_from_rfid(
            {"tag_id": "TAG-1", "plate": "AAA-111"},
            "iot-rfid-gate-a",
            utc_now(),
            from_local_metres(11.0, 10.5, ORIGIN),
            ORIGIN,
        )
    )
    assert len(fusion.entities) == 1
    entity = next(iter(fusion.entities.values()))
    assert {device.split(":", 1)[0] for device in entity.device_ids} == {"gps", "tag"}


def test_a_camera_track_can_still_join_a_gps_entity() -> None:
    """Device exclusivity must not block the multi-sensor case, which is the point of fusion.

    A camera observation carries no device id, so there is nothing to conflict with.
    """
    fusion = SensorFusion(ORIGIN, assoc_radius_m=25.0)
    fusion.observe(gps_observation(40.0, 20.0, device="gps-truck-9"))
    fusion.observe(camera_observation(41.5, 21.0, source="cam-gate-a", track="trk-1", seconds=1))

    assert len(fusion.entities) == 1
    entity = next(iter(fusion.entities.values()))
    assert entity.modalities == {"gps", "video"}
    assert entity.is_multi_sensor


# --------------------------------------------------------------- entity merging
def test_a_camera_entity_and_a_gps_entity_merge_into_one() -> None:
    """Track-to-track fusion.

    Association happens one observation at a time, so a truck first seen by a camera becomes a
    video-only entity while its GPS tracker becomes a second one — and once the camera track is bound
    it stays bound, so the two never meet. Live, that produced one entity per *sensor* rather than one
    per truck, with multi_sensor stuck at 1 of 8 while everything looked superficially fine.
    """
    fusion = SensorFusion(ORIGIN, assoc_radius_m=25.0)

    # The mechanism that keeps them apart is not distance — it is that each sensor's *index* lookup
    # short-circuits before positional matching. A camera track bound to one entity and a GPS device
    # bound to another will stay bound however close the two entities drift, which is exactly what
    # happened live: one entity per sensor rather than one per truck.
    fusion.observe(camera_observation(30.0, 20.0, track="trk-1", sigma=4.0))
    fusion.observe(gps_observation(90.0, 20.0, device="gps-truck-5"))  # far away: a separate entity
    assert len(fusion.entities) == 2

    # Now the truck drives into the camera's view: both sensors report the same place, but each is
    # already bound, so neither association path can bring them together.
    for step in range(4):
        fusion.observe(
            camera_observation(30.0 + step * 0.2, 20.0, track="trk-1", sigma=4.0, seconds=1 + step)
        )
        fusion.observe(
            gps_observation(31.0 + step * 0.2, 20.2, device="gps-truck-5", seconds=1 + step)
        )
    assert len(fusion.entities) == 2, "still two, because both sensors are index-bound"

    assert fusion.merge_pass() == 1
    assert len(fusion.entities) == 1

    entity = next(iter(fusion.entities.values()))
    assert entity.modalities == {"gps", "video"}, "the merged entity must claim both sensors"
    assert entity.is_multi_sensor
    assert entity.device_ids == {"gps:gps-truck-5"}
    assert entity.track_ids == {"trk-1"}
    assert entity.observations == 10, "observation counts add up"


def test_merging_re_points_the_indexes() -> None:
    """After a merge, the absorbed entity's device and track ids must resolve to the survivor.

    Otherwise every later observation from that device finds a dangling id, creates a fresh entity,
    and the pair re-splits on the next message — a merge loop that looks like flapping.
    """
    fusion = SensorFusion(ORIGIN, assoc_radius_m=25.0)
    fusion.observe(camera_observation(10.0, 10.0, track="trk-9", sigma=4.0))
    fusion.observe(gps_observation(90.0, 10.0, device="gps-9"))
    for step in range(3):
        fusion.observe(camera_observation(10.0, 10.0, track="trk-9", sigma=4.0, seconds=1 + step))
        fusion.observe(gps_observation(11.0, 10.2, device="gps-9", seconds=1 + step))
    assert len(fusion.entities) == 2
    assert fusion.merge_pass() == 1
    survivor = next(iter(fusion.entities))

    fusion.observe(gps_observation(12.3, 10.2, device="gps-9", seconds=3))
    fusion.observe(camera_observation(11.0, 10.0, track="trk-9", sigma=4.0, seconds=3))
    assert len(fusion.entities) == 1, "both sensors must still resolve to the survivor"
    assert next(iter(fusion.entities)) == survivor


def test_two_different_trackers_never_merge() -> None:
    """The safety property. Merging two entities destroys information that cannot be recovered."""
    fusion = SensorFusion(ORIGIN, assoc_radius_m=25.0)
    fusion.observe(gps_observation(20.0, 20.0, device="gps-a"))
    fusion.observe(gps_observation(90.0, 20.0, device="gps-b"))
    # Both bound, then parked two metres apart — closer than any gate would refuse.
    for step in range(4):
        fusion.observe(gps_observation(20.0, 20.0, device="gps-a", seconds=1 + step))
        fusion.observe(gps_observation(22.0, 20.0, device="gps-b", seconds=1 + step))
    assert len(fusion.entities) == 2
    assert fusion.merge_pass() == 0, "two GPS trackers are two objects, whatever the distance"
    assert len(fusion.entities) == 2


def test_a_person_and_a_truck_never_merge() -> None:
    fusion = SensorFusion(ORIGIN, assoc_radius_m=25.0)
    fusion.observe(camera_observation(5.0, 5.0, label="truck", track="t-truck"))
    fusion.observe(camera_observation(5.2, 5.1, label="truck", track="t-truck", seconds=1))
    fusion.observe(camera_observation(6.0, 5.0, label="person", track="t-person", seconds=1))
    fusion.observe(camera_observation(6.1, 5.1, label="person", track="t-person", seconds=2))
    assert fusion.merge_pass() == 0
    assert len(fusion.entities) == 2


def test_modalities_survive_the_evidence_window() -> None:
    """`provenance` is a bounded window; the claim "GPS and video agreed" is durable."""
    fusion = SensorFusion(ORIGIN)
    fusion.observe(camera_observation(0.0, 0.0, track="t1", sigma=4.0))
    for step in range(60):  # flood the window with GPS fixes
        fusion.observe(gps_observation(0.1 * step, 0.0, device="gps-1", seconds=step * 0.1))
    fusion.merge_pass()
    entity = next(iter(fusion.entities.values()))
    assert len(entity.provenance) <= 40, "the evidence window stays bounded"
    assert "video" in entity.modalities, "but the camera contribution is not forgotten"
    assert entity.is_multi_sensor


# ------------------------------------------------------------------ operator labels
def test_a_fleet_number_reads_like_a_fleet_number() -> None:
    """A map label must not leak association plumbing.

    Live, the drone was labelled "Drone gps:gps-drone-0018": the namespace is an internal detail of
    device identity, and repeating the type is noise on a crowded map.
    """
    from sio_fusion.service import _fleet_number

    assert _fleet_number("gps:gps-drone-0018") == "0018"
    assert _fleet_number("tag:TAG-ABC-123") == "123"
    assert _fleet_number("gps:tracker") == "tracker", "nothing to strip: leave it alone"
    assert _fleet_number("gps:a-b") == "a-b", "a two-character tail is not a fleet number"
