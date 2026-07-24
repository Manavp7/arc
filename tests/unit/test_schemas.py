"""Contract tests for ``sio_schemas``.

These guard the wire format. If one of them fails, a producer and a consumer somewhere are
about to disagree.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from sio_schemas import (
    SCHEMA_VERSION,
    Alert,
    AuditRecord,
    BBox,
    BusMessage,
    Decision,
    DecisionOption,
    Detection,
    Entity,
    EntityState,
    Event,
    EventType,
    Explanation,
    Forecast,
    ForecastPoint,
    Geo,
    HealthStatus,
    Mission,
    Observation,
    Principal,
    Relationship,
    RelationshipType,
    Severity,
    SimulationRun,
    Topic,
    Track,
    TrackState,
    Velocity,
    WorkflowRun,
    new_id,
    utc_now,
)
from sio_schemas.enums import ActionType, Modality


# --------------------------------------------------------------------------- ids
def test_new_id_is_prefixed_and_sortable() -> None:
    first = new_id("obs")
    second = new_id("obs")
    assert first.startswith("obs_")
    assert len(first) == len(second)
    # ULID-shaped ids must sort in creation order; timeline tie-breaking relies on it.
    assert first < second or first[:14] == second[:14]


def test_ids_are_unique_under_load() -> None:
    ids = {new_id("det") for _ in range(5_000)}
    assert len(ids) == 5_000


# -------------------------------------------------------------------- timestamps
def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError, match="naive datetime"):
        Observation(source_id="cam-a", modality=Modality.VIDEO, ts=datetime(2026, 7, 24, 12, 0))


def test_timestamp_accepts_iso_z_epoch_and_millis() -> None:
    expected = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    for value in (
        "2026-07-24T12:00:00Z",
        "2026-07-24T12:00:00+00:00",
        expected.timestamp(),
        expected.timestamp() * 1000,
    ):
        obs = Observation(source_id="cam-a", modality=Modality.IOT, ts=value)
        assert obs.ts == expected


def test_timestamps_are_normalised_to_utc() -> None:
    obs = Observation(source_id="cam-a", modality=Modality.GPS, ts="2026-07-24T14:00:00+02:00")
    assert obs.ts.utcoffset() == timedelta(0)
    assert obs.ts.hour == 12


# ------------------------------------------------------------------- strictness
def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Observation(source_id="cam-a", modality=Modality.VIDEO, typo_field=1)


def test_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Detection(observation_id="o", **{"class": "truck"}, confidence=1.4, source_id="cam-a")


# ------------------------------------------------------------------ wire aliases
def test_detection_class_uses_the_prd_wire_name() -> None:
    detection = Detection(
        observation_id="obs_1", **{"class": "fire"}, confidence=0.8, source_id="cam-a"
    )
    wire = detection.to_wire()
    assert "class" in wire and "class_name" not in wire
    assert Detection.model_validate(wire).class_name == "fire"


def test_relationship_uses_from_and_to_on_the_wire() -> None:
    rel = Relationship(**{"from": "ent_a", "to": "ent_b"}, type=RelationshipType.SEEN_BY)
    wire = rel.to_wire()
    assert wire["from"] == "ent_a" and wire["to"] == "ent_b"
    assert Relationship.model_validate(wire).from_id == "ent_a"


def test_every_exported_model_round_trips_through_json() -> None:
    """Serialise → JSON → validate must be lossless for every bus payload."""
    from sio_schemas.export import EXPORTED

    samples: dict[str, object] = {
        "Observation": Observation(source_id="cam-a", modality=Modality.VIDEO),
        "Detection": Detection(
            observation_id="obs_1", **{"class": "truck"}, confidence=0.9, source_id="cam-a"
        ),
        "Track": Track(**{"class": "truck"}, source_id="cam-a"),
        "Entity": Entity(type="truck", label="T-1"),
        "Relationship": Relationship(**{"from": "a", "to": "b"}, type=RelationshipType.ENTERED),
        "Event": Event(type=EventType.FIRE_DETECTED, severity=Severity.CRITICAL),
        "Forecast": Forecast(target="congestion", horizon_s=600),
        "Decision": Decision(rationale="test"),
        "Alert": Alert(title="t", group_key="g"),
        "Explanation": Explanation(),
        "BusMessage": BusMessage(topic="events", kind="Event"),
        "Mission": Mission(name="Yard sweep"),
        "WorkflowRun": WorkflowRun(playbook="fire_response"),
        "SimulationRun": SimulationRun(scenario="fire_spread"),
        "AuditRecord": AuditRecord(actor="operator@example.com", action="entities.list"),
        "Principal": Principal(subject="operator@example.com", tenant_id="default"),
        "HealthStatus": HealthStatus(service="api", schema_version=SCHEMA_VERSION),
    }
    assert set(samples) >= {m.__name__ for m in EXPORTED}, "every exported contract needs a sample"
    for model_cls in EXPORTED:
        sample = samples[model_cls.__name__]
        payload = json.loads(sample.to_json())  # type: ignore[attr-defined]
        again = model_cls.model_validate(payload)
        assert json.loads(again.to_json()) == payload, model_cls.__name__


# ------------------------------------------------------------------------- geo
def test_geo_distance_and_bearing() -> None:
    a = Geo(lat=37.7749, lon=-122.4194)
    b = a.offset(north_m=100, east_m=0)
    assert 99 < a.distance_to(b) < 101
    assert a.bearing_to(b) == pytest.approx(0.0, abs=1.0)
    east = a.offset(north_m=0, east_m=100)
    assert a.bearing_to(east) == pytest.approx(90.0, abs=1.0)


def test_geo_range_validation() -> None:
    with pytest.raises(ValidationError):
        Geo(lat=91.0, lon=0.0)


def test_velocity_derives_speed_and_heading() -> None:
    v = Velocity(north=3.0, east=4.0)
    assert v.speed_mps == pytest.approx(5.0)
    assert v.speed_kmh == pytest.approx(18.0)
    assert v.heading_deg == pytest.approx(53.13, abs=0.1)


def test_bbox_geometry_and_iou() -> None:
    a = BBox(x1=0, y1=0, x2=10, y2=10)
    b = BBox(x1=5, y1=5, x2=15, y2=15)
    assert a.area == 100
    assert a.center == (5.0, 5.0)
    assert a.iou(b) == pytest.approx(25 / 175)
    assert a.iou(BBox(x1=20, y1=20, x2=30, y2=30)) == 0.0
    assert a.clip(8, 8).x2 == 8
    # expand() grows every side but never produces negative pixel coordinates, so a box on
    # the frame edge grows only inward — ReID crops must stay inside the image.
    inner = BBox(x1=10, y1=10, x2=20, y2=20)
    assert inner.expand(0.1).width == pytest.approx(12.0)
    assert a.expand(0.1).x1 == 0.0
    assert a.expand(0.1).width == pytest.approx(11.0)


def test_bbox_rejects_inverted_corners() -> None:
    with pytest.raises(ValidationError, match="out of order"):
        BBox(x1=10, y1=0, x2=1, y2=10)


# ----------------------------------------------------------------- world model
def test_entity_dwell_and_sources() -> None:
    from sio_schemas import Provenance

    start = utc_now()
    entity = Entity(
        type="truck",
        first_seen=start,
        last_seen=start + timedelta(minutes=20),
        provenance=[
            Provenance(source_id="cam-gate-a", modality=Modality.VIDEO, ts=start),
            Provenance(source_id="gps-truck-1", modality=Modality.GPS, ts=start),
            Provenance(source_id="cam-gate-a", modality=Modality.VIDEO, ts=start),
        ],
    )
    assert entity.dwell_s() == pytest.approx(1200)
    assert set(entity.sources) == {"cam-gate-a", "gps-truck-1"}


def test_relationship_bitemporality() -> None:
    """A closed edge still answers questions about the past — the basis of replay (UC5)."""
    t0 = utc_now()
    rel = Relationship(
        **{"from": "truck", "to": "dock-3"},
        type=RelationshipType.CONTAINS,
        ts_valid_from=t0,
        ts_valid_to=t0 + timedelta(minutes=10),
    )
    assert not rel.is_open
    assert rel.holds_at(t0 + timedelta(minutes=5))
    assert not rel.holds_at(t0 - timedelta(minutes=1))
    assert not rel.holds_at(t0 + timedelta(minutes=11))


def test_relationship_rejects_reversed_validity() -> None:
    now = utc_now()
    with pytest.raises(ValidationError, match="precedes"):
        Relationship(
            **{"from": "a", "to": "b"},
            type=RelationshipType.NEAR,
            ts_valid_from=now,
            ts_valid_to=now - timedelta(seconds=1),
        )


def test_track_helpers() -> None:
    now = utc_now()
    track = Track(
        **{"class": "truck"},
        source_id="cam-a",
        start_ts=now,
        last_ts=now + timedelta(seconds=30),
        states=[
            TrackState(ts=now, geo=Geo(lat=1, lon=1)),
            TrackState(ts=now + timedelta(seconds=30), geo=Geo(lat=1.001, lon=1)),
        ],
    )
    assert track.duration_s == pytest.approx(30)
    assert len(track.trajectory()) == 2
    assert track.latest is not None and track.latest.geo is not None


# -------------------------------------------------------------------- reasoning
def test_severity_is_ordered_by_rank_not_alphabet() -> None:
    assert Severity.CRITICAL.rank > Severity.HIGH.rank > Severity.MEDIUM.rank
    assert Severity.LOW < Severity.CRITICAL
    # Alphabetically "critical" < "low"; the enum must not fall for that.
    assert sorted([Severity.LOW, Severity.CRITICAL], key=lambda s: s.rank)[-1] is Severity.CRITICAL


def test_event_records_detection_latency() -> None:
    now = utc_now()
    event = Event(type=EventType.SPEEDING, ts=now, detected_ts=now + timedelta(seconds=2.5))
    assert event.detection_latency_s == pytest.approx(2.5)


def test_decision_requires_a_valid_chosen_option() -> None:
    option = DecisionOption(action=ActionType.CLOSE_GATE, score=1.0, expected_effect="gate shut")
    decision = Decision(options=[option], chosen=option.option_id)
    assert decision.chosen_option is option
    with pytest.raises(ValidationError, match="not among the options"):
        Decision(options=[option], chosen="opt_missing")


def test_decision_is_only_actionable_after_approval() -> None:
    from sio_schemas import ApprovalState

    option = DecisionOption(action=ActionType.DISPATCH_DRONE, score=2.0, expected_effect="eyes on")
    pending = Decision(options=[option], chosen=option.option_id)
    assert not pending.is_actionable, "human-on-the-loop gate must block unapproved actions"
    approved = pending.model_copy(update={"approval": ApprovalState.APPROVED})
    assert approved.is_actionable


def test_forecast_interval_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="lo exceeds hi"):
        ForecastPoint(ts=utc_now(), value=1.0, lo=5.0, hi=2.0)


def test_explanation_emptiness() -> None:
    assert Explanation().is_empty
    assert not Explanation(related_entities=["ent_1"]).is_empty


# -------------------------------------------------------------------------- bus
def test_bus_message_wraps_and_inherits_trace_and_tenant() -> None:
    detection = Detection(
        observation_id="obs_1",
        **{"class": "person"},
        confidence=0.7,
        source_id="cam-b",
        tenant_id="acme",
    )
    message = BusMessage.of(Topic.DETECTIONS, detection, producer="perception")
    assert message.topic == "detections"
    assert message.kind == "Detection"
    assert message.tenant_id == "acme"
    assert message.trace_id == detection.trace_id
    assert message.schema_version == SCHEMA_VERSION
    assert message.decode(Detection).id == detection.id


def test_bus_message_decode_auto_and_unknown_kind() -> None:
    event = Event(type=EventType.DWELL_EXCEEDED)
    message = BusMessage.of(Topic.EVENTS, event, producer="events")
    assert isinstance(message.decode_auto(), Event)
    # An unknown payload type must be skippable, not fatal: a newer producer may publish
    # something an older consumer has never heard of.
    assert BusMessage(topic="events", kind="FutureThing").decode_auto() is None


def test_stream_id_is_never_published() -> None:
    message = BusMessage(topic="events", kind="Event", stream_id="1-1")
    assert "stream_id" not in json.loads(message.to_json())


def test_topic_helpers() -> None:
    assert Topic.RAW_FRAMES in Topic.raw_topics()
    assert Topic.EVENTS not in Topic.raw_topics()
    assert Topic.EVENTS.dead_letter == "dlq.events"


# ---------------------------------------------------------------- json schemas
def test_json_schema_export_covers_every_bus_payload(tmp_path: object) -> None:
    from pathlib import Path

    from sio_schemas.export import EXPORTED, write_all

    out = Path(str(tmp_path)) / "schemas"
    written = write_all(out)
    assert len(written) == len(EXPORTED) + 1  # +1 for index.json
    index = json.loads((out / "index.json").read_text())
    assert index["schema_version"] == SCHEMA_VERSION
    detection_schema = json.loads((out / "Detection.json").read_text())
    assert "class" in detection_schema["properties"], "schema must use PRD wire names"


def test_entity_state_accepts_covariance_for_uncertainty_display() -> None:
    state = EntityState(geo=Geo(lat=1, lon=2), covariance=[1.0, 0.0, 0.0, 1.0])
    assert state.covariance is not None and len(state.covariance) == 4
