"""Tests for the explanation builder (PRD M20).

"Explainable by default" only holds if the explanation is actually populated, ordered and
honest about uncertainty — that is what these assert.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from sio_core.explain import ExplanationBuilder, merge_explanations
from sio_schemas import (
    Detection,
    Entity,
    Event,
    EventType,
    EvidenceKind,
    Observation,
    Provenance,
    Severity,
    Track,
    utc_now,
)
from sio_schemas.enums import Modality


def test_evidence_and_sources_accumulate() -> None:
    detection = Detection(
        observation_id="obs_1", **{"class": "fire"}, confidence=0.82, source_id="cam-dock-3"
    )
    explanation = ExplanationBuilder("Fire detected at dock 3").add_detection(detection).build()

    assert explanation.summary == "Fire detected at dock 3"
    assert explanation.evidence[0].kind is EvidenceKind.DETECTION
    assert explanation.evidence[0].ref == detection.id
    assert explanation.sources == ["cam-dock-3"]
    assert explanation.timeline[0].kind == "detection"
    assert not explanation.is_empty


def test_observation_contributes_its_raw_frame_as_evidence() -> None:
    """An operator who distrusts an answer wants the frame, not a summary of the frame."""
    observation = Observation(
        source_id="cam-gate-a",
        modality=Modality.VIDEO,
        raw_ref="frames/cam-gate-a/000042.jpg",
        confidence=0.99,
    )
    explanation = ExplanationBuilder().add_observation(observation).build()
    kinds = {e.kind for e in explanation.evidence}
    assert EvidenceKind.OBSERVATION in kinds
    assert EvidenceKind.FRAME in kinds
    assert any(e.ref.endswith("000042.jpg") for e in explanation.evidence)


def test_entity_adds_related_and_sources_without_duplicates() -> None:
    now = utc_now()
    entity = Entity(
        type="truck",
        label="Truck ABC-123",
        provenance=[
            Provenance(source_id="cam-gate-a", modality=Modality.VIDEO, ts=now),
            Provenance(source_id="cam-gate-a", modality=Modality.VIDEO, ts=now),
            Provenance(source_id="gps-truck-1", modality=Modality.GPS, ts=now),
        ],
    )
    explanation = ExplanationBuilder().add_entity(entity).add_entity(entity).build()
    assert explanation.related_entities == [entity.entity_id]
    assert sorted(explanation.sources) == ["cam-gate-a", "gps-truck-1"]


def test_timeline_is_sorted_chronologically_regardless_of_insertion_order() -> None:
    now = utc_now()
    explanation = (
        ExplanationBuilder()
        .add_timeline(now + timedelta(minutes=5), "event", "later")
        .add_timeline(now, "detection", "earlier")
        .add_timeline(now + timedelta(minutes=1), "note", "middle")
        .build()
    )
    assert [entry.summary for entry in explanation.timeline] == ["earlier", "middle", "later"]


def test_query_evidence_records_what_was_actually_run() -> None:
    explanation = (
        ExplanationBuilder()
        .add_query("MATCH (t:Entity {type:'truck'}) RETURN t", backend="neo4j", rows=3)
        .build()
    )
    evidence = explanation.evidence[0]
    assert evidence.kind is EvidenceKind.QUERY
    assert "MATCH" in evidence.ref
    assert evidence.note == "neo4j, 3 rows"


def test_confidence_is_noisy_or_over_evidence() -> None:
    """Two independent 0.8 signals should raise belief, but never to certainty."""
    high = Detection(observation_id="o", **{"class": "fire"}, confidence=0.8, source_id="cam-a")
    other = Detection(observation_id="o", **{"class": "smoke"}, confidence=0.8, source_id="cam-b")
    single = ExplanationBuilder().add_detection(high).build()
    both = ExplanationBuilder().add_detection(high).add_detection(other).build()

    assert single.confidence == pytest.approx(0.8)
    assert both.confidence > single.confidence
    assert both.confidence == pytest.approx(0.96, abs=0.01)
    assert both.confidence <= 0.99, "nothing in a sensor system is certain"


def test_explicit_confidence_overrides_the_derived_value() -> None:
    detection = Detection(observation_id="o", **{"class": "x"}, confidence=0.9, source_id="cam-a")
    explanation = ExplanationBuilder().add_detection(detection).confidence(0.25).build()
    assert explanation.confidence == 0.25


def test_confidence_with_no_evidence_is_zero() -> None:
    assert ExplanationBuilder().build().confidence == 0.0
    assert ExplanationBuilder().build().is_empty


def test_alternatives_are_recorded() -> None:
    explanation = (
        ExplanationBuilder()
        .add_alternative("sunlight glare on metal", confidence=0.2, why_not="thermal sensor agrees")
        .build()
    )
    assert explanation.alternatives[0].hypothesis == "sunlight glare on metal"
    assert explanation.alternatives[0].why_not == "thermal sensor agrees"


def test_degraded_marks_and_explains_the_fallback() -> None:
    """A degraded answer must announce itself rather than quietly look normal."""
    explanation = ExplanationBuilder().degraded("LLM tool-calling failed").build()
    assert explanation.degraded is True
    assert any("degraded" in note for note in explanation.notes)


def test_track_and_event_and_rule_and_model_evidence() -> None:
    track = Track(**{"class": "truck"}, source_id="cam-a", hits=12)
    event = Event(type=EventType.DWELL_EXCEEDED, severity=Severity.MEDIUM, entities=["ent_1"])
    explanation = (
        ExplanationBuilder()
        .add_track(track)
        .add_event(event)
        .add_rule("dwell_exceeded", note="threshold 900s")
        .add_model("yolo26n.onnx", note="conf 0.35")
        .build()
    )
    kinds = [e.kind for e in explanation.evidence]
    assert EvidenceKind.TRACK in kinds
    assert EvidenceKind.EVENT in kinds
    assert EvidenceKind.RULE in kinds
    assert EvidenceKind.MODEL in kinds
    assert "ent_1" in explanation.related_entities


def test_merge_preserves_all_evidence_and_degradation() -> None:
    """Alert grouping merges several events; losing evidence there would break the audit."""
    first = (
        ExplanationBuilder("first")
        .add_evidence(EvidenceKind.EVENT, "evt_1", score=0.7)
        .add_source("cam-a")
        .add_related("ent_1")
        .build()
    )
    second = (
        ExplanationBuilder("second")
        .add_evidence(EvidenceKind.EVENT, "evt_2", score=0.6)
        .add_source("cam-b")
        .add_related("ent_2")
        .degraded("no LLM available")
        .build()
    )

    merged = merge_explanations([first, second], summary="two events, one incident")

    assert merged.summary == "two events, one incident"
    assert {e.ref for e in merged.evidence} == {"evt_1", "evt_2"}
    assert sorted(merged.sources) == ["cam-a", "cam-b"]
    assert sorted(merged.related_entities) == ["ent_1", "ent_2"]
    assert merged.degraded is True
    assert merged.confidence > max(first.confidence, second.confidence)
