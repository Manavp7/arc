"""Tests for alerting (PRD M16).

An alerts inbox lives or dies on its ordering. Get it wrong and the inbox becomes a list nobody reads — at
which point the alerting is *worse* than none, because it has converted a real signal into noise people have
learned to dismiss.

So most of these tests are about ranking and folding, and several encode orderings that a human triaging by
hand would consider obvious: a real intrusion above a chattering speed sensor, a fresh critical above a stale
one, and an alert that keeps repeating still ageing so that it can escalate.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sio_alerts import (
    DEDUP_WINDOW_S,
    ESCALATE_AFTER_S,
    group_key,
    recency_factor,
    score_alert,
    should_escalate,
    title_for,
    within_dedup_window,
    zone_criticality,
)

from sio_schemas import AlertState, Event, EventType, Explanation, Severity, utc_now


def an_event(**kwargs: Any) -> Event:
    defaults: dict[str, Any] = {
        "tenant_id": "acme",
        "type": EventType.FIRE_DETECTED,
        "severity": Severity.CRITICAL,
        "confidence": 0.9,
        "zone_id": "dock_3",
    }
    defaults.update(kwargs)
    return Event(**defaults)


def score_of(**kwargs: Any) -> float:
    defaults: dict[str, Any] = {
        "severity": "high",
        "confidence": 0.9,
        "zone_id": "dock_3",
        "last_ts": utc_now(),
    }
    defaults.update(kwargs)
    return score_alert(**defaults).score


# ---------------------------------------------------------------------- ranking
def test_severity_dominates_the_ranking() -> None:
    assert score_of(severity="critical") > score_of(severity="high") > score_of(severity="medium")


def test_severity_is_a_rank_so_two_mediums_cannot_outrank_a_critical() -> None:
    assert score_of(severity="critical") > 2 * score_of(severity="medium")


def test_confidence_scales_the_score() -> None:
    """A 40 %-confidence fire is worth raising and is not worth the same as a certain one."""
    assert score_of(confidence=0.4) < score_of(confidence=0.95)
    assert score_of(confidence=0.4) > 0, "an uncertain critical still belongs in the inbox"


def test_where_it_happened_changes_the_priority() -> None:
    """A fire in the fuel store is not the event a fire in the car park is, and nothing upstream knows
    that — the events engine sees a detection, not a site plan."""
    assert score_of(zone_id="fuel_store") > score_of(zone_id="car_park")
    assert zone_criticality("fuel_store") > zone_criticality(None)
    assert zone_criticality("somewhere_unlisted") == zone_criticality(None)


def test_a_stale_alert_sinks_without_disappearing() -> None:
    """Decay, not a cut-off: an unacknowledged critical from this morning still belongs in the inbox, below
    the fresh ones. An alert that decays to nothing has been silently resolved by the passage of time, which
    is not a thing that happens."""
    fresh = score_of(severity="critical", last_ts=utc_now())
    old = score_of(severity="critical", last_ts=utc_now() - timedelta(hours=6))
    assert old < fresh
    assert old > 0
    assert recency_factor(utc_now() - timedelta(days=7)) >= 0.05


def test_a_chattering_sensor_cannot_outrank_a_real_intrusion() -> None:
    """The ordering flaw the shipped weights actually produced.

    Measured: fifty medium-severity speeding events scored 19.7 while a fresh high-severity intrusion scored
    15.1 — a stuck detector outranking a real event, which is precisely what teaches operators to stop
    reading an inbox. Repetition may lift an alert TOWARD the next severity class, never past it.
    """
    chattering = score_of(severity="medium", count=50, zone_id="lane_north")
    intrusion = score_of(severity="high", zone_id="warehouse")
    assert chattering < intrusion, f"chattering {chattering} outranked intrusion {intrusion}"


def test_repetition_still_matters_within_a_severity() -> None:
    """The cap must not make repetition meaningless: fifty occurrences is worse than one."""
    assert score_of(severity="medium", count=50) > score_of(severity="medium", count=1)
    assert score_of(severity="critical", count=20) > score_of(severity="critical", count=1)


def test_repetition_is_damped_not_linear() -> None:
    once = score_of(severity="medium", count=1)
    fifty = score_of(severity="medium", count=50)
    assert fifty < once * 50, "linear repetition would let noise dominate everything"


def test_the_score_explains_itself() -> None:
    """ "Why is this at the top" is the first question an operator asks, and "the algorithm decided" ends the
    conversation badly."""
    scored = score_alert(
        severity="critical", confidence=0.45, zone_id="fuel_store", last_ts=utc_now(), count=4
    )
    assert "critical severity" in scored.reason
    assert "45% confidence" in scored.reason
    assert "fuel_store" in scored.reason
    assert "4 occurrences" in scored.reason
    assert set(scored.factors) == {
        "severity",
        "confidence",
        "zone_criticality",
        "recency",
        "repetition",
    }


# ---------------------------------------------------------------------- grouping
def test_repeats_of_the_same_thing_share_a_group() -> None:
    first = an_event(zone_id="dock_3")
    second = an_event(zone_id="dock_3")
    assert group_key(first) == group_key(second)


def test_the_same_event_type_in_different_places_does_not_fold() -> None:
    """Too coarse fails badly: two genuinely different fires folding into one alert gets resolved once."""
    assert group_key(an_event(zone_id="dock_3")) != group_key(an_event(zone_id="fuel_store"))


def test_a_fire_is_one_alert_however_many_entities_are_near_it() -> None:
    """A fire is about a place. Folding on the entity would give one alert per truck near the same fire."""
    assert group_key(an_event(entities=["ent_a"])) == group_key(an_event(entities=["ent_b"]))


def test_a_per_entity_event_folds_per_entity() -> None:
    """Speeding is about a vehicle: two trucks speeding are two problems."""
    first = an_event(type=EventType.SPEEDING, severity=Severity.MEDIUM, entities=["ent_a"])
    second = an_event(type=EventType.SPEEDING, severity=Severity.MEDIUM, entities=["ent_b"])
    assert group_key(first) != group_key(second)


def test_the_dedup_window_is_bounded() -> None:
    assert within_dedup_window(utc_now())
    assert not within_dedup_window(utc_now() - timedelta(seconds=DEDUP_WINDOW_S + 60))


# ------------------------------------------------------------------------ titles
def test_a_title_prefers_the_events_own_summary() -> None:
    """Written by the component that knew what happened, which beats anything reconstructed here."""
    event = an_event(
        explanation=Explanation(summary="Flame detected by cam-dock-3-4 with confidence 0.63")
    )
    assert title_for(event).startswith("Flame detected")


def test_a_title_without_a_summary_still_scans() -> None:
    """An inbox of identical titles cannot be scanned at all."""
    title = title_for(an_event(explanation=Explanation()))
    assert "Fire Detected" in title
    assert "dock_3" in title


# -------------------------------------------------------------------- escalation
def test_an_unacknowledged_critical_escalates() -> None:
    escalate, reason = should_escalate(
        severity="critical",
        state=AlertState.OPEN,
        ts=utc_now() - timedelta(seconds=ESCALATE_AFTER_S["critical"] + 30),
        ack_ts=None,
    )
    assert escalate
    assert reason and "unacknowledged" in reason


def test_acknowledging_stops_the_escalation_timer() -> None:
    """Escalation is about whether a human is engaged, not about the event getting worse."""
    escalate, _ = should_escalate(
        severity="critical",
        state=AlertState.OPEN,
        ts=utc_now() - timedelta(hours=2),
        ack_ts=utc_now() - timedelta(minutes=1),
    )
    assert not escalate


def test_an_already_escalated_alert_does_not_escalate_again() -> None:
    escalate, _ = should_escalate(
        severity="critical",
        state=AlertState.ESCALATED,
        ts=utc_now() - timedelta(hours=2),
        ack_ts=None,
    )
    assert not escalate


def test_a_medium_alert_does_not_escalate_on_a_timer() -> None:
    """Escalating everything is the same as escalating nothing."""
    escalate, _ = should_escalate(
        severity="medium", state=AlertState.OPEN, ts=utc_now() - timedelta(days=1), ack_ts=None
    )
    assert not escalate


def test_a_critical_escalates_sooner_than_a_high() -> None:
    assert ESCALATE_AFTER_S["critical"] < ESCALATE_AFTER_S["high"]


def test_a_fresh_alert_does_not_escalate_immediately() -> None:
    escalate, _ = should_escalate(
        severity="critical", state=AlertState.OPEN, ts=utc_now(), ack_ts=None
    )
    assert not escalate


# ------------------------------------------------------------------- what alerts
def test_only_medium_and_above_reach_the_inbox() -> None:
    """An inbox containing every zone entry is an inbox nobody opens — and then the criticals in it are
    invisible too."""
    from sio_alerts.service import ALERTABLE

    assert Severity.INFO not in ALERTABLE
    assert Severity.LOW not in ALERTABLE
    assert Severity.MEDIUM in ALERTABLE
    assert Severity.CRITICAL in ALERTABLE


def test_the_ordering_a_human_would_choose() -> None:
    """One test for the whole ranking, written as the order a person triaging by hand would pick."""
    now = utc_now()
    ranked = sorted(
        [
            (
                "critical fire in the fuel store, now",
                score_of(severity="critical", zone_id="fuel_store", confidence=0.95, last_ts=now),
            ),
            (
                "high intrusion in the warehouse, now",
                score_of(severity="high", zone_id="warehouse", last_ts=now),
            ),
            (
                "uncertain critical in the car park",
                score_of(severity="critical", zone_id="car_park", confidence=0.4, last_ts=now),
            ),
            (
                "medium speeding, fifty times",
                score_of(severity="medium", count=50, zone_id="lane_north", last_ts=now),
            ),
            (
                "medium speeding, once",
                score_of(severity="medium", zone_id="lane_north", last_ts=now),
            ),
        ],
        key=lambda pair: -pair[1],
    )
    labels = [label for label, _ in ranked]
    assert labels[0] == "critical fire in the fuel store, now"
    assert labels.index("high intrusion in the warehouse, now") < labels.index(
        "medium speeding, fifty times"
    )
    assert labels[-1] == "medium speeding, once"
