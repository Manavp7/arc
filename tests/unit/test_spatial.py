"""Tests for spatial reasoning (PRD M6).

Two things get the most attention, because they are where this component fails in the field rather
than in a demo:

* **hysteresis.** The naive "inside now, outside before → entered" rule produces an event storm for an
  entity parked on a boundary. Most of these tests are about *not* emitting events.
* **agreement between implementations.** Membership is decided in memory on the hot path and in PostGIS
  for ad-hoc queries. Two implementations of point-in-polygon that quietly disagree is a bug that
  surfaces as an inexplicable timeline, so the shapes are tested directly here and cross-checked
  against PostGIS in the infra suite.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from shapely.geometry import Polygon
from sio_spatial.geometry import (
    DEFAULT_H3_RESOLUTION,
    CameraFootprint,
    ZoneIndex,
    ZoneShape,
    cell_for,
    cells_within,
    haversine_m,
    metres_to_degrees,
    zone_shape_from_row,
)
from sio_spatial.membership import MembershipTracker

from sio_schemas import Geo, utc_now

ORIGIN = Geo(lat=37.7749, lon=-122.4194)


def square(centre: Geo, side_m: float) -> Polygon:
    """An axis-aligned square of a given side length in metres, centred on a point."""
    half_lat, half_lon = metres_to_degrees(side_m / 2, centre.lat)
    return Polygon(
        [
            (centre.lon - half_lon, centre.lat - half_lat),
            (centre.lon + half_lon, centre.lat - half_lat),
            (centre.lon + half_lon, centre.lat + half_lat),
            (centre.lon - half_lon, centre.lat + half_lat),
        ]
    )


def a_zone(
    zone_id: str = "dock", centre: Geo = ORIGIN, side_m: float = 60.0, *, restricted: bool = False
) -> ZoneShape:
    return ZoneShape(
        zone_id=zone_id,
        name=zone_id.replace("_", " ").title(),
        kind="dock",
        restricted=restricted,
        polygon=square(centre, side_m),
    )


def offset(geo: Geo, *, east_m: float = 0.0, north_m: float = 0.0) -> Geo:
    lat_degrees, lon_degrees = metres_to_degrees(1.0, geo.lat)
    return Geo(lat=geo.lat + north_m * lat_degrees, lon=geo.lon + east_m * lon_degrees)


# --------------------------------------------------------------------- geometry
def test_metres_to_degrees_round_trips_through_haversine() -> None:
    for metres in (1.0, 25.0, 500.0):
        moved = offset(ORIGIN, north_m=metres)
        assert haversine_m(ORIGIN, moved) == pytest.approx(metres, rel=0.01)
        moved_east = offset(ORIGIN, east_m=metres)
        assert haversine_m(ORIGIN, moved_east) == pytest.approx(metres, rel=0.01)


def test_a_point_inside_a_zone_is_inside_it() -> None:
    zone = a_zone(side_m=60)
    assert zone.contains(ORIGIN)
    assert not zone.contains(offset(ORIGIN, east_m=100))


def test_distance_to_boundary_is_signed() -> None:
    """Hysteresis depends on this sign, so it gets its own test."""
    zone = a_zone(side_m=60)
    assert zone.distance_to_boundary_m(ORIGIN) == pytest.approx(30.0, rel=0.05), "centre is 30 m in"
    just_inside = offset(ORIGIN, east_m=28)
    assert 0 < zone.distance_to_boundary_m(just_inside) < 3
    just_outside = offset(ORIGIN, east_m=32)
    assert -3 < zone.distance_to_boundary_m(just_outside) < 0


def test_nested_zones_return_innermost_first() -> None:
    """A restricted cage inside a yard: the most specific answer is the smallest enclosing zone.

    Returning only one zone would make "is this person in a restricted area?" depend on insertion
    order, which is not a property anyone should have to reason about.
    """
    index = ZoneIndex([a_zone("yard", side_m=200), a_zone("cage", side_m=20, restricted=True)])
    containing = index.zones_containing(ORIGIN)
    assert [zone.zone_id for zone in containing] == ["cage", "yard"]
    assert index.innermost(ORIGIN).zone_id == "cage"
    assert index.innermost(ORIGIN).restricted


def test_a_point_outside_everything_is_in_no_zone() -> None:
    index = ZoneIndex([a_zone(side_m=40)])
    assert index.zones_containing(offset(ORIGIN, east_m=500)) == []
    assert index.innermost(offset(ORIGIN, east_m=500)) is None


def test_nearest_zone_can_filter_by_kind() -> None:
    far = ZoneShape(
        zone_id="gate",
        name="Gate",
        kind="gate",
        restricted=False,
        polygon=square(offset(ORIGIN, east_m=150), 20),
    )
    index = ZoneIndex([a_zone("dock", side_m=40), far])
    assert index.nearest(ORIGIN)[0].zone_id == "dock"
    assert index.nearest(ORIGIN, kind="gate")[0].zone_id == "gate"
    assert index.nearest(ORIGIN, kind="runway") is None


def test_zone_shape_from_geojson_row() -> None:
    row = {
        "zone_id": "dock_1",
        "name": "Dock 1",
        "kind": "dock",
        "restricted": False,
        "geojson": {"type": "Polygon", "coordinates": [list(square(ORIGIN, 40).exterior.coords)]},
    }
    shape_ = zone_shape_from_row(row)
    assert shape_ is not None
    assert shape_.contains(ORIGIN)
    assert zone_shape_from_row({"zone_id": "x", "geojson": None}) is None


# --------------------------------------------------------------------------- H3
def test_h3_cells_are_stable_and_distinct() -> None:
    assert cell_for(ORIGIN) == cell_for(ORIGIN), "the same point must always give the same cell"
    assert cell_for(ORIGIN) != cell_for(offset(ORIGIN, east_m=200)), (
        "200 m apart is a different cell"
    )


def test_h3_resolution_is_sized_for_vehicles() -> None:
    """A cell should hold about one vehicle, or counts per cell mean nothing.

    Too coarse puts the whole dock apron in one bucket; too fine scatters a single truck across a
    dozen cells and turns every count into noise.
    """
    import h3

    area = h3.average_hexagon_area(DEFAULT_H3_RESOLUTION, unit="m^2")
    assert 100 < area < 1_000, f"resolution {DEFAULT_H3_RESOLUTION} gives {area:.0f} m2 cells"


def test_cells_within_a_radius_cover_it_without_overreaching() -> None:
    cells = cells_within(ORIGIN, 50.0)
    assert cell_for(ORIGIN) in cells, "the origin's own cell must be included"
    from sio_spatial.geometry import cell_centre

    assert all(haversine_m(ORIGIN, cell_centre(cell)) <= 50.0 for cell in cells)
    assert len(cells_within(ORIGIN, 200.0)) > len(cells), "a bigger radius covers more cells"


def test_ring_count_follows_the_resolution() -> None:
    """Derived from the resolution's edge length, so changing resolution does not silently change
    what "within 500 m" means."""
    fine = cells_within(ORIGIN, 100.0, 12)
    coarse = cells_within(ORIGIN, 100.0, 10)
    assert len(fine) > len(coarse)


# ------------------------------------------------------------------- footprints
def test_a_camera_footprint_is_a_sector_not_a_triangle() -> None:
    """A triangle understates the far edge of a wide lens by about 8 per cent of its range, which
    fabricates blind spots that do not exist."""
    footprint = CameraFootprint.build("cam", ORIGIN, bearing_deg=0.0, fov_deg=70.0, range_m=60.0)
    straight_ahead = offset(ORIGIN, north_m=55)
    assert footprint.covers(straight_ahead)
    # A point near the arc's edge, which a chord would cut off.
    edge = offset(ORIGIN, north_m=50, east_m=25)
    assert footprint.covers(edge), "the sector must reach the arc, not the chord"
    assert not footprint.covers(offset(ORIGIN, north_m=-30)), "behind the camera"
    assert not footprint.covers(offset(ORIGIN, north_m=200)), "beyond its range"


def test_a_footprint_respects_its_bearing() -> None:
    east_facing = CameraFootprint.build("cam", ORIGIN, bearing_deg=90.0, fov_deg=60.0, range_m=50.0)
    assert east_facing.covers(offset(ORIGIN, east_m=40))
    assert not east_facing.covers(offset(ORIGIN, north_m=40))


# ------------------------------------------------------------------ hysteresis
def tracker(**kwargs: float) -> MembershipTracker:
    defaults = {"margin_m": 2.0, "enter_confirm_s": 2.0, "exit_grace_s": 15.0}
    defaults.update(kwargs)
    return MembershipTracker(index=ZoneIndex([a_zone("dock", side_m=60)]), **defaults)  # type: ignore[arg-type]


def test_a_clean_entry_and_exit_produce_exactly_two_events() -> None:
    """The happy path: drive in, stay, drive out."""
    tracked = tracker()
    start = utc_now()

    assert tracked.observe("truck-1", ORIGIN, start) == [], "provisional, not yet confirmed"
    entered = tracked.observe("truck-1", ORIGIN, start + timedelta(seconds=3))
    assert [change.kind for change in entered] == ["entered"]
    assert entered[0].zone_id == "dock"

    # Parked for a minute. Dwell is measured from the observations, so they have to happen.
    for second in range(6, 60, 3):
        assert tracked.observe("truck-1", ORIGIN, start + timedelta(seconds=second)) == []

    outside = offset(ORIGIN, east_m=100)
    assert tracked.observe("truck-1", outside, start + timedelta(seconds=62)) == [], "grace period"
    exited = tracked.observe("truck-1", outside, start + timedelta(seconds=90))
    assert [change.kind for change in exited] == ["exited"]
    # Dwell runs to the last sighting INSIDE (t=57), not to the moment the exit was confirmed. All we
    # actually know is that it left somewhere between 57 s and 62 s, and the conservative end of that
    # is the honest number to report.
    assert exited[0].dwell_s == pytest.approx(57.0, abs=1.0)


def test_an_entry_is_timestamped_when_it_happened_not_when_it_was_confirmed() -> None:
    """The confirmation delay is an artefact of how we decide, not part of the world.

    Letting it leak into the timestamp would make every dwell measurement short by the confirmation
    window — a systematic bias, which is worse than noise because averaging cannot remove it.
    """
    tracked = tracker(enter_confirm_s=5.0)
    start = utc_now()
    tracked.observe("truck-1", ORIGIN, start)
    changes = tracked.observe("truck-1", ORIGIN, start + timedelta(seconds=6))
    assert changes[0].ts == start


def test_an_entity_jittering_on_a_boundary_produces_no_events() -> None:
    """The failure this whole component exists to prevent.

    A truck parked on a dock boundary with a couple of metres of GPS noise reports
    inside/outside/inside indefinitely. Without hysteresis the events table would claim it entered and
    left dozens of times, and every downstream rule would inherit that.
    """
    tracked = tracker()
    start = utc_now()
    events = []
    for step in range(60):
        # Alternating half a metre either side of the boundary: well inside the 2 m margin.
        east = 29.5 if step % 2 else 30.5
        events += tracked.observe(
            "truck-1", offset(ORIGIN, east_m=east), start + timedelta(seconds=step)
        )

    assert events == [], f"boundary jitter produced {len(events)} events"


def test_clipping_a_corner_is_not_an_entry() -> None:
    """A vehicle turning through the corner of a zone has not entered it."""
    tracked = tracker(enter_confirm_s=3.0)
    start = utc_now()
    changes = tracked.observe("truck-1", ORIGIN, start)
    changes += tracked.observe("truck-1", offset(ORIGIN, east_m=200), start + timedelta(seconds=1))
    assert changes == []
    assert tracked.stats["entries_discarded"] == 1
    assert tracked.occupancy() == {}, "and it must not appear as an occupant"


def test_a_dropped_fix_is_not_a_departure() -> None:
    """A false exit is worse than a late one: it closes the dwell clock and can fire rules about
    leaving, on a truck that never moved."""
    tracked = tracker(exit_grace_s=20.0)
    start = utc_now()
    tracked.observe("truck-1", ORIGIN, start)
    tracked.observe("truck-1", ORIGIN, start + timedelta(seconds=3))

    # One bad fix throwing the position across the site, then recovery.
    assert (
        tracked.observe("truck-1", offset(ORIGIN, east_m=300), start + timedelta(seconds=10)) == []
    )
    assert tracked.observe("truck-1", ORIGIN, start + timedelta(seconds=12)) == []
    assert tracked.zones_of("truck-1") == ["dock"], "still inside throughout"
    assert tracked.stats["exited"] == 0


def test_a_sustained_absence_does_become_a_departure() -> None:
    """The grace period must be a delay, not a veto."""
    tracked = tracker(exit_grace_s=10.0)
    start = utc_now()
    tracked.observe("truck-1", ORIGIN, start)
    tracked.observe("truck-1", ORIGIN, start + timedelta(seconds=3))
    outside = offset(ORIGIN, east_m=300)
    tracked.observe("truck-1", outside, start + timedelta(seconds=20))
    changes = tracked.observe("truck-1", outside, start + timedelta(seconds=35))
    assert [change.kind for change in changes] == ["exited"]


def test_nested_zones_produce_an_event_each() -> None:
    """Entering a restricted cage inside a yard is two facts, and the restricted one matters."""
    tracked = MembershipTracker(
        index=ZoneIndex([a_zone("yard", side_m=200), a_zone("cage", side_m=20, restricted=True)]),
        enter_confirm_s=1.0,
    )
    start = utc_now()
    tracked.observe("person-1", ORIGIN, start)
    changes = tracked.observe("person-1", ORIGIN, start + timedelta(seconds=2))
    assert sorted(change.zone_id for change in changes) == ["cage", "yard"]
    assert any(change.restricted for change in changes)
    assert sorted(tracked.zones_of("person-1")) == ["cage", "yard"]


def test_occupancy_excludes_provisional_entries() -> None:
    """An occupancy count that includes unconfirmed entries would flicker exactly as much as the
    events it is meant to replace."""
    tracked = tracker(enter_confirm_s=5.0)
    start = utc_now()
    tracked.observe("truck-1", ORIGIN, start)
    assert tracked.occupancy() == {}
    tracked.observe("truck-1", ORIGIN, start + timedelta(seconds=6))
    assert tracked.occupancy() == {"dock": ["truck-1"]}


def test_forgetting_an_entity_closes_its_membership_at_the_last_known_time() -> None:
    """An entity that stops being observed has not necessarily left, but leaving the membership open
    forever makes occupancy drift upward permanently."""
    tracked = tracker()
    start = utc_now()
    tracked.observe("truck-1", ORIGIN, start)
    tracked.observe("truck-1", ORIGIN, start + timedelta(seconds=3))
    last_seen = start + timedelta(seconds=3)

    changes = tracked.forget("truck-1", start + timedelta(seconds=500))
    assert [change.kind for change in changes] == ["exited"]
    assert changes[0].ts == last_seen, "the exit is recorded when we last actually knew, not now"
    assert tracked.occupancy() == {}


def test_stale_memberships_expire() -> None:
    tracked = tracker()
    start = utc_now()
    tracked.observe("truck-1", ORIGIN, start)
    tracked.observe("truck-1", ORIGIN, start + timedelta(seconds=3))
    assert tracked.expire_stale(start + timedelta(seconds=30), max_silence_s=60.0) == []
    changes = tracked.expire_stale(start + timedelta(seconds=300), max_silence_s=60.0)
    assert [change.kind for change in changes] == ["exited"]


def test_two_entities_in_one_zone_are_tracked_separately() -> None:
    tracked = tracker()
    start = utc_now()
    for entity in ("truck-1", "truck-2"):
        tracked.observe(entity, ORIGIN, start)
        tracked.observe(entity, ORIGIN, start + timedelta(seconds=3))
    assert sorted(tracked.occupancy()["dock"]) == ["truck-1", "truck-2"]

    tracked.observe("truck-1", offset(ORIGIN, east_m=300), start + timedelta(seconds=10))
    tracked.observe("truck-1", offset(ORIGIN, east_m=300), start + timedelta(seconds=40))
    assert tracked.occupancy()["dock"] == ["truck-2"]


def test_dwell_accumulates_while_inside() -> None:
    tracked = tracker()
    start = utc_now()
    tracked.observe("truck-1", ORIGIN, start)
    for minute in range(1, 6):
        tracked.observe("truck-1", ORIGIN, start + timedelta(minutes=minute))
    assert tracked.dwell_of("truck-1", "dock") == pytest.approx(300.0, abs=1.0)
    assert tracked.dwell_of("truck-1", "nowhere") is None
    assert tracked.dwell_of("ghost", "dock") is None


def test_hysteresis_stats_are_reported() -> None:
    """An operator asking "why did that not fire?" needs to see the suppression counts."""
    tracked = tracker()
    start = utc_now()
    tracked.observe("truck-1", ORIGIN, start)
    tracked.observe("truck-1", ORIGIN, start + timedelta(seconds=3))
    tracked.observe("truck-1", offset(ORIGIN, east_m=300), start + timedelta(seconds=5))
    assert tracked.stats["provisional_entries"] == 1
    assert tracked.stats["entered"] == 1
    assert tracked.stats["exits_deferred"] >= 1


# ------------------------------------------------ the service's publish path
class RecordingContext:
    """Captures what a handler publishes, so the publish path can be tested without a bus."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    async def publish(self, topic: str, payload: object) -> None:
        self.published.append((str(topic), payload))


async def test_a_confirmed_entry_publishes_an_event_and_opens_an_edge() -> None:
    """Exercise the real publish path.

    The tracker tests above all passed while the service raised `RelationshipType has no attribute
    LOCATED_IN` on every single entry — 80 events went out and not one edge was opened, because nothing
    tested the code between the tracker and the bus. A component is not covered until the path that
    *uses* it runs.
    """
    from sio_spatial.service import SpatialService

    from sio_schemas import Entity, EntityType, Provenance

    service = SpatialService()
    service.index.replace([a_zone("dock_1", side_m=60), a_zone("yard", side_m=400)])
    service.tracker = MembershipTracker(service.index, enter_confirm_s=1.0, exit_grace_s=5.0)

    entity = Entity(
        entity_id="ent_test",
        tenant_id="default",
        type=EntityType.TRUCK,
        label="Truck ABC-123",
        provenance=[Provenance(source_id="gps-truck-1", modality="gps", ts=utc_now())],
    )
    ctx = RecordingContext()
    start = utc_now()

    service.tracker.observe(entity.entity_id, ORIGIN, start)
    changes = service.tracker.observe(entity.entity_id, ORIGIN, start + timedelta(seconds=2))
    assert changes, "the tracker should have confirmed the entry"
    for change in changes:
        await service._publish_change(change, entity, ctx)  # type: ignore[arg-type]

    events = [payload for topic, payload in ctx.published if topic == "events"]
    edges = [payload for topic, payload in ctx.published if topic == "entities"]
    assert len(events) == 2, "one event per zone entered (dock and yard)"
    assert len(edges) == 2, "and one open edge per zone"
    assert service._edges_opened == 2
    assert len(service._open_edges) == 2

    event = events[0]
    assert str(event.type) in ("zone_entered", "unauthorized_entry")
    assert event.entities == ["ent_test"]
    assert event.explanation.summary and "Truck ABC-123" in event.explanation.summary
    assert event.explanation.notes, "an event with no explanation is not actionable"
    assert event.rule_id == "spatial.zone_entered"

    edge = edges[0]
    assert str(edge.type) == "entered", "the visit is one bitemporal edge, opened at entry"
    assert edge.ts_valid_to is None, "still open"
    assert edge.from_id == "ent_test"


async def test_an_exit_closes_the_same_edge_rather_than_opening_another() -> None:
    """Closing the interval is what makes "where was it at T?" a single query."""
    from sio_spatial.service import SpatialService

    from sio_schemas import Entity, EntityType

    service = SpatialService()
    service.index.replace([a_zone("dock_1", side_m=60)])
    service.tracker = MembershipTracker(service.index, enter_confirm_s=1.0, exit_grace_s=5.0)
    entity = Entity(entity_id="ent_x", tenant_id="default", type=EntityType.TRUCK)
    ctx = RecordingContext()
    start = utc_now()

    service.tracker.observe("ent_x", ORIGIN, start)
    for change in service.tracker.observe("ent_x", ORIGIN, start + timedelta(seconds=2)):
        await service._publish_change(change, entity, ctx)  # type: ignore[arg-type]

    outside = offset(ORIGIN, east_m=200)
    service.tracker.observe("ent_x", outside, start + timedelta(seconds=5))
    for change in service.tracker.observe("ent_x", outside, start + timedelta(seconds=20)):
        await service._publish_change(change, entity, ctx)  # type: ignore[arg-type]

    edges = [payload for topic, payload in ctx.published if topic == "entities"]
    assert len(edges) == 2, "the same edge, published open then closed"
    assert edges[0].id == edges[1].id, "closing must reuse the edge id, not mint a new one"
    assert edges[0].ts_valid_to is None
    assert edges[1].ts_valid_to is not None
    assert service._edges_closed == 1
    assert service._open_edges == {}


async def test_an_exit_with_no_open_edge_is_skipped_not_invented() -> None:
    """The entry predates this process. Fabricating a start time would corrupt the very history that
    bitemporal storage exists to protect."""
    from sio_spatial.membership import MembershipChange
    from sio_spatial.service import SpatialService

    from sio_schemas import Entity, EntityType

    service = SpatialService()
    service.index.replace([a_zone("dock_1", side_m=60)])
    ctx = RecordingContext()

    await service._publish_change(  # type: ignore[arg-type]
        MembershipChange(
            entity_id="ent_ghost",
            zone_id="dock_1",
            kind="exited",
            ts=utc_now(),
            dwell_s=120.0,
            zone_name="Dock 1",
        ),
        Entity(entity_id="ent_ghost", tenant_id="default", type=EntityType.TRUCK),
        ctx,
    )
    events = [payload for topic, payload in ctx.published if topic == "events"]
    edges = [payload for topic, payload in ctx.published if topic == "entities"]
    assert len(events) == 1, "the exit still happened and is still worth recording"
    assert edges == [], "but no edge is invented"
    assert service._edges_closed == 0


async def test_entering_a_restricted_zone_raises_the_severity() -> None:
    from sio_spatial.service import SpatialService

    from sio_schemas import Entity, EntityType, Severity

    service = SpatialService()
    service.index.replace([a_zone("cage", side_m=40, restricted=True)])
    service.tracker = MembershipTracker(service.index, enter_confirm_s=1.0)
    entity = Entity(entity_id="ent_p", tenant_id="default", type=EntityType.PERSON)
    ctx = RecordingContext()
    start = utc_now()

    service.tracker.observe("ent_p", ORIGIN, start)
    for change in service.tracker.observe("ent_p", ORIGIN, start + timedelta(seconds=2)):
        await service._publish_change(change, entity, ctx)  # type: ignore[arg-type]

    event = next(payload for topic, payload in ctx.published if topic == "events")
    assert str(event.type) == "unauthorized_entry"
    assert event.severity == Severity.HIGH
    assert any("restricted" in note for note in event.explanation.notes)


async def test_an_expired_membership_publishes_an_inferred_exit() -> None:
    """An expiry that is computed and discarded is worse than one never computed.

    The first version logged a line, popped the edge and told nobody. The consequence was invisible in
    this service and severe two services downstream: large enclosing zones are only ever LEFT by expiry —
    an entity inside the site is inside the perimeter and the yard until it stops being observed — so the
    perimeter accumulated 54 entries and ZERO exits, every edge stayed open forever, and the prediction
    service reported 41 entities on a dock apron that holds a handful, then forecast it rising.
    """
    from sio_spatial.service import SpatialService

    from sio_schemas import Entity, EntityType

    service = SpatialService()
    service.index.replace([a_zone("perimeter", side_m=400)])
    service.tracker = MembershipTracker(service.index, enter_confirm_s=1.0)
    ctx = RecordingContext()
    start = utc_now()

    entity = Entity(entity_id="ent_gone", tenant_id="default", type=EntityType.TRUCK)
    service.tracker.observe("ent_gone", ORIGIN, start)
    for change in service.tracker.observe("ent_gone", ORIGIN, start + timedelta(seconds=2)):
        await service._publish_change(change, entity, ctx)  # type: ignore[arg-type]
    assert len(service._open_edges) == 1

    # Nothing reports it again, and the membership expires.
    expired = service.tracker.expire_stale(start + timedelta(seconds=600), max_silence_s=120.0)
    assert [change.kind for change in expired] == ["exited"]

    published_before = len(ctx.published)
    service.publish = ctx.publish  # type: ignore[assignment,method-assign]
    for change in expired:
        await service._publish_expiry(change)

    new = ctx.published[published_before:]
    events = [payload for topic, payload in new if topic == "events"]
    edges = [payload for topic, payload in new if topic == "entities"]
    assert len(events) == 1, "the exit must be published, not merely logged"
    assert len(edges) == 1, "and the visit interval must be closed"
    assert edges[0].ts_valid_to is not None
    assert service._open_edges == {}

    event = events[0]
    assert str(event.type) == "zone_exited"
    assert event.attributes["inferred"] is True
    assert event.confidence < 0.9, "an inference deserves less confidence than an observation"
    assert any(
        "stopped being reported" in note or "no longer" in note or "silence" in note
        for note in [*event.explanation.notes, event.explanation.summary or ""]
    )
    assert any("indistinguishable" in note for note in event.explanation.notes), (
        "it may have left or its tracker may have failed, and the record should say so"
    )


async def test_an_inferred_exit_names_the_entity_rather_than_its_id() -> None:
    """An expiry has no Entity to hand — nothing reported it, which is the whole point.

    Without a label cache the feed reads "ent_01KYBNWZZR1DWFBMPR3ZSW30CW is no longer tracked in Fuel
    store", which is technically complete and useless to the person reading it. Observed in the console
    during a replay.
    """
    from sio_schemas import Entity, EntityType
    from sio_spatial.service import SpatialService

    service = SpatialService()
    service.index.replace([a_zone("fuel_store", side_m=60)])
    service.tracker = MembershipTracker(service.index, enter_confirm_s=1.0)
    ctx = RecordingContext()
    service.publish = ctx.publish  # type: ignore[assignment,method-assign]
    start = utc_now()

    entity = Entity(
        entity_id="ent_long_id_nobody_can_read",
        tenant_id="default",
        type=EntityType.TRUCK,
        label="Truck ABC-123",
    )
    # Drive the real on_message path so the label cache is filled the way production fills it, rather
    # than by poking the attribute — the point of the test is that the caching happens.
    from sio_schemas import BusMessage, EntityState, Geo, Topic

    positioned = entity.model_copy(
        update={"state": EntityState(ts=start, geo=ORIGIN, confidence=0.9)}
    )
    await service.on_message(BusMessage.of(Topic.ENTITIES, positioned), ctx)  # type: ignore[arg-type]
    assert service._labels[entity.entity_id] == "Truck ABC-123"

    later = positioned.model_copy(
        update={
            "state": EntityState(ts=start + timedelta(seconds=2), geo=ORIGIN, confidence=0.9),
        }
    )
    await service.on_message(BusMessage.of(Topic.ENTITIES, later), ctx)  # type: ignore[arg-type]

    for change in service.tracker.expire_stale(start + timedelta(seconds=600), max_silence_s=60.0):
        await service._publish_expiry(change)

    exits = [
        payload
        for topic, payload in ctx.published
        if topic == "events" and payload.attributes.get("inferred")
    ]
    assert exits, "the expiry must publish an event"
    assert "Truck ABC-123" in (exits[0].explanation.summary or "")
    assert entity.entity_id not in (exits[0].explanation.summary or ""), (
        "the raw id is not what a human reads"
    )
    # And the cache must not grow forever: entity ids are minted per run.
    assert entity.entity_id not in service._labels
