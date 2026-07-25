"""Tests for the event engine (PRD M9, M22).

The headline requirement is that **adding a rule requires no core change**, so the most important test
here writes a brand-new rule to a temporary directory as YAML and asserts it fires — with no Python
written, no class registered and no import added.

The rest concentrate on the ways a rule engine becomes useless in production:

* firing constantly (no cooldown) until someone deletes the rule;
* firing on a single noisy sample instead of a sustained condition;
* being unable to notice something that *stopped* happening;
* being taken down entirely by one malformed rule file;
* producing alerts nobody can act on because the reason is not recorded.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sio_events.anomaly import FeatureVector, RobustZScoreDetector, build_detector
from sio_events.engine import RuleEngine
from sio_events.facts import (
    Fact,
    fact_from_detection,
    fact_from_entity,
    fact_from_event,
    fact_from_observation,
)
from sio_events.rules import Condition, Rule, RuleSet, load_rules

from sio_schemas import (
    BBox,
    Detection,
    Entity,
    EntityState,
    EntityType,
    Event,
    EventType,
    Geo,
    Modality,
    Observation,
    Severity,
    Velocity,
    utc_now,
)

RULES_DIR = Path("infra/rules")


def a_fact(kind: str = "entity", ts: datetime | None = None, **fields: object) -> Fact:
    return Fact(
        kind=kind,
        ts=ts or utc_now(),
        fields=dict(fields),
        entity_id=fields.get("entity_id"),  # type: ignore[arg-type]
        zone_id=fields.get("zone_id"),  # type: ignore[arg-type]
        source_id=fields.get("source_id"),  # type: ignore[arg-type]
    )


def engine_from(*rules: dict[str, object]) -> RuleEngine:
    return RuleEngine(RuleSet(rules=[Rule.model_validate(rule) for rule in rules]))


# ------------------------------------------------------------------- conditions
@pytest.mark.parametrize(
    ("op", "value", "actual", "expected"),
    [
        ("eq", 5, 5, True),
        ("eq", 5, 6, False),
        ("eq", "high", "HIGH", True),  # case-insensitive: YAML authors are human
        ("eq", 20, "20", True),  # a string field and an int rule mean the same thing
        ("gte", 20, 31.4, True),
        ("gte", 20, 19.9, False),
        ("lt", 0.1, 0.0, True),
        ("in", ["fire", "flame"], "flame", True),
        ("in", ["fire", "flame"], "smoke", False),
        ("not_in", ["fire"], "smoke", True),
        ("contains", "dock", "dock_3", True),
        ("contains", "truck", ["truck", "person"], True),
        ("matches", "^dock", "dock_3", True),
        ("matches", "^dock", "lane_south", False),
        ("exists", None, 0, True),
        ("exists", None, None, False),
        ("missing", None, None, True),
    ],
)
def test_condition_operators(op: str, value: object, actual: object, expected: bool) -> None:
    assert (
        Condition.model_validate({"field": "x", "op": op, "value": value}).evaluate(actual)
        is expected
    )


def test_operator_symbols_are_accepted() -> None:
    """A rule author writing ">=" and getting a validation error learns nothing useful."""
    assert Condition.model_validate({"field": "x", "op": ">=", "value": 5}).op == "gte"
    assert Condition.model_validate({"field": "x", "op": "==", "value": 5}).op == "eq"
    assert Condition.model_validate({"field": "x", "op": "=~", "value": "^a"}).op == "matches"


def test_a_condition_never_crashes_on_a_type_mismatch() -> None:
    """A typo in YAML must be a rule that does not match, not an outage."""
    condition = Condition.model_validate({"field": "x", "op": "gte", "value": 20})
    assert condition.evaluate("not a number") is False
    assert condition.evaluate(None) is False
    assert condition.evaluate([1, 2, 3]) is False


def test_a_condition_describes_itself_with_the_actual_value() -> None:
    """ "speed_kmh gte 20" is barely better than nothing; the value is what makes it checkable."""
    condition = Condition.model_validate({"field": "speed_kmh", "op": "gte", "value": 20})
    described = condition.describe(31.4)
    assert "31.4" in described and "20" in described


# ------------------------------------------------------------------------ facts
def test_a_fact_exposes_a_dotted_path_and_tolerates_absence() -> None:
    fact = a_fact(zone_id="dock_1", attributes={"plate": "ABC-123", "nested": {"deep": 7}})
    assert fact.get("zone_id") == "dock_1"
    assert fact.get("attributes.plate") == "ABC-123"
    assert fact.get("attributes.nested.deep") == 7
    assert fact.get("attributes.missing") is None
    assert fact.get("nothing.at.all") is None, "a missing path must not raise"


def test_entity_facts_expose_speed_in_both_units() -> None:
    """A yard speed limit is posted in km/h. Making every rule author divide by 3.6 is how a rule ends
    up wrong by a factor of 3.6."""
    entity = Entity(
        entity_id="ent_1",
        tenant_id="t",
        type=EntityType.TRUCK,
        state=EntityState(
            ts=utc_now(), geo=Geo(lat=1.0, lon=2.0), velocity=Velocity(east=10.0, north=0.0)
        ),
    )
    fact = fact_from_entity(entity)
    assert fact.get("speed_mps") == pytest.approx(10.0)
    assert fact.get("speed_kmh") == pytest.approx(36.0, abs=0.1)
    assert fact.get("entity_type") == "truck"
    assert fact.get("lat") == 1.0


def test_observation_facts_expose_the_payload() -> None:
    observation = Observation(
        tenant_id="t",
        source_id="iot-temp-1",
        modality=Modality.IOT,
        ts=utc_now(),
        payload={"metric": "temperature_c", "value": 71.5, "zone_id": "warehouse", "forced": True},
    )
    fact = fact_from_observation(observation)
    assert fact.get("metric") == "temperature_c"
    assert fact.get("value") == 71.5
    assert fact.get("payload.forced") is True
    assert fact.zone_id == "warehouse"


def test_event_facts_allow_rules_about_events() -> None:
    """Composition. `zone_breach` narrows the spatial service's `unauthorized_entry` by entity type,
    rather than re-deriving geometry in a second place."""
    event = Event(
        tenant_id="t",
        type=EventType.UNAUTHORIZED_ENTRY,
        severity=Severity.HIGH,
        entities=["ent_1"],
        zone_id="fuel_store",
        attributes={"restricted": True, "entity_type": "truck", "dwell_s": 950},
    )
    fact = fact_from_event(event)
    assert fact.get("event_type") == "unauthorized_entry"
    assert fact.get("restricted") is True
    assert fact.get("entity_type") == "truck"
    assert fact.get("dwell_s") == 950


def test_detection_facts_carry_class_under_both_names() -> None:
    detection = Detection(
        tenant_id="t",
        source_id="cam-1",
        observation_id="obs_1",
        ts=utc_now(),
        **{"class": "fire"},
        confidence=0.8,
        bbox=BBox(x1=0, y1=0, x2=10, y2=20),
    )
    fact = fact_from_detection(detection)
    assert fact.get("class") == "fire"
    assert fact.get("class_name") == "fire"
    assert fact.get("area_px") == 200
    # Aspect ratio is exposed because person_fell needs it, and YAML cannot compute it.
    assert fact.get("aspect_ratio") == pytest.approx(0.5)


# ------------------------------------------------------- the M22 acceptance test
def test_a_brand_new_rule_needs_no_code_change(tmp_path: Path) -> None:
    """PRD M22, asserted literally.

    A rule that did not exist when this service was written, expressed only as YAML, fires. No Python
    written, no class registered, no import added — which is the whole point, because a rule that
    requires a deploy is a rule that does not get written.
    """
    (tmp_path / "invented.yaml").write_text(
        """
rules:
  - id: purple_forklift_in_the_office
    emits: zone_breach
    severity: high
    description: A rule nobody anticipated, added as data.
    kinds: [entity]
    when:
      - field: entity_type
        op: eq
        value: forklift
      - field: attributes.colour
        op: eq
        value: purple
      - field: zone_id
        op: eq
        value: office
    cooldown_seconds: 0
    explanation: A purple forklift is in {zone_id}, which should never happen.
"""
    )
    ruleset = load_rules(tmp_path)
    assert ruleset.errors == []
    engine = RuleEngine(ruleset)

    matches = engine.evaluate(
        a_fact(entity_type="forklift", zone_id="office", attributes={"colour": "purple"})
    )
    assert len(matches) == 1
    assert matches[0].rule.id == "purple_forklift_in_the_office"
    assert matches[0].reasons, "and it explains itself"

    # A near miss must not fire.
    assert (
        engine.evaluate(
            a_fact(entity_type="forklift", zone_id="office", attributes={"colour": "yellow"})
        )
        == []
    )


def test_the_shipped_rules_all_load() -> None:
    """A malformed rule that ships is a rule that silently does nothing."""
    ruleset = load_rules(RULES_DIR)
    assert ruleset.errors == [], f"shipped rules failed to load: {ruleset.errors}"
    assert len(ruleset.rules) >= 12, "the PRD names twelve rules"
    ids = {rule.id for rule in ruleset.rules}
    for required in (
        "unauthorized_entry_person",
        "fire_detected",
        "speeding",
        "crowd_gathering",
        "machine_stopped",
        "power_failure",
        "forced_door",
        "person_fell",
        "abandoned_package",
        "suspicious_meeting",
        "dwell_exceeded",
        "zone_breach",
    ):
        assert required in ids, f"missing required rule: {required}"


def test_every_shipped_rule_emits_a_known_event_type() -> None:
    """A rule emitting a typo'd type would fire into a category nothing displays."""
    for rule in load_rules(RULES_DIR).rules:
        EventType(rule.emits)  # raises on an unknown type


def test_every_shipped_rule_has_a_cooldown_and_a_description() -> None:
    """A rule with no cooldown fires on every message about a parked truck; one with no description is
    unmaintainable by whoever inherits it."""
    for rule in load_rules(RULES_DIR).rules:
        assert rule.cooldown_seconds > 0, f"{rule.id} has no cooldown"
        assert rule.description.strip(), f"{rule.id} has no description"


def test_one_malformed_rule_does_not_stop_the_others(tmp_path: Path) -> None:
    """A YAML typo is routine. A loader that raises on the first problem means one bad file silently
    disables the fire rule."""
    (tmp_path / "good.yaml").write_text(
        "rules:\n  - id: fine\n    emits: speeding\n    kinds: [entity]\n"
    )
    (tmp_path / "bad.yaml").write_text(
        "rules:\n  - id: broken\n    emits: speeding\n    nonsense_key: true\n"
    )
    (tmp_path / "unparseable.yaml").write_text("rules: [ this is not: valid: yaml")

    ruleset = load_rules(tmp_path)
    assert [rule.id for rule in ruleset.rules] == ["fine"]
    assert len(ruleset.errors) == 2
    assert any("broken" in error or "nonsense" in error for error in ruleset.errors)


def test_duplicate_rule_ids_are_rejected_not_silently_resolved(tmp_path: Path) -> None:
    """ "The last one wins" is a rule nobody can see when reading either file."""
    (tmp_path / "a.yaml").write_text("rules:\n  - id: dup\n    emits: speeding\n")
    (tmp_path / "b.yaml").write_text("rules:\n  - id: dup\n    emits: congestion\n")
    ruleset = load_rules(tmp_path)
    assert len(ruleset.rules) == 1
    assert any("duplicate" in error for error in ruleset.errors)


def test_a_rule_cannot_declare_two_shapes() -> None:
    with pytest.raises(ValueError, match="one shape"):
        Rule.model_validate(
            {
                "id": "confused",
                "emits": "speeding",
                "window": {"aggregate": "count", "seconds": 10},
                "absence": {"after_seconds": 60},
            }
        )


# -------------------------------------------------------------------- cooldown
def test_cooldown_stops_an_event_per_message() -> None:
    """Without this, one stationary truck in a restricted zone produces an event per second until
    someone deletes the rule."""
    engine = engine_from(
        {
            "id": "restricted",
            "emits": "unauthorized_entry",
            "kinds": ["entity"],
            "when": [{"field": "zone_id", "op": "eq", "value": "cage"}],
            "cooldown_seconds": 60,
            "cooldown_key": ["entity_id"],
        }
    )
    start = utc_now()
    fired = 0
    for second in range(120):
        fired += len(
            engine.evaluate(
                a_fact(entity_id="ent_1", zone_id="cage", ts=start + timedelta(seconds=second))
            )
        )
    assert fired == 2, (
        f"120 messages over two minutes with a 60 s cooldown should fire twice, got {fired}"
    )
    assert engine.stats["suppressed"] == 118


def test_cooldown_is_per_subject() -> None:
    """One truck's cooldown must not silence a different truck."""
    engine = engine_from(
        {
            "id": "restricted",
            "emits": "unauthorized_entry",
            "kinds": ["entity"],
            "when": [{"field": "zone_id", "op": "eq", "value": "cage"}],
            "cooldown_seconds": 600,
            "cooldown_key": ["entity_id"],
        }
    )
    assert len(engine.evaluate(a_fact(entity_id="ent_1", zone_id="cage"))) == 1
    assert len(engine.evaluate(a_fact(entity_id="ent_2", zone_id="cage"))) == 1
    assert len(engine.evaluate(a_fact(entity_id="ent_1", zone_id="cage"))) == 0


# ---------------------------------------------------------------- window rules
def test_a_window_rule_needs_a_sustained_condition() -> None:
    """Speeding uses the peak over ten seconds, because one noisy fix can put a parked truck at
    40 km/h and a rule that believes a single sample will cry wolf until it is switched off."""
    engine = engine_from(
        {
            "id": "speeding",
            "emits": "speeding",
            "kinds": ["entity"],
            "when": [{"field": "speed_kmh", "op": "gte", "value": 20}],
            "window": {
                "aggregate": "count",
                "seconds": 10,
                "group_by": ["entity_id"],
                "op": "gte",
                "value": 3,
            },
            "cooldown_seconds": 0,
            "cooldown_key": ["entity_id"],
        }
    )
    start = utc_now()
    # One outlier: does not fire.
    assert engine.evaluate(a_fact(entity_id="e1", speed_kmh=45.0, ts=start)) == []
    # Sustained: fires on the third.
    assert (
        engine.evaluate(a_fact(entity_id="e1", speed_kmh=31.0, ts=start + timedelta(seconds=1)))
        == []
    )
    matches = engine.evaluate(
        a_fact(entity_id="e1", speed_kmh=28.0, ts=start + timedelta(seconds=2))
    )
    assert len(matches) == 1
    assert matches[0].aggregate_value == 3.0


def test_a_window_forgets_facts_older_than_its_span() -> None:
    engine = engine_from(
        {
            "id": "sustained",
            "emits": "speeding",
            "kinds": ["entity"],
            "when": [{"field": "speed_kmh", "op": "gte", "value": 20}],
            "window": {
                "aggregate": "count",
                "seconds": 10,
                "group_by": ["entity_id"],
                "op": "gte",
                "value": 3,
            },
            "cooldown_seconds": 0,
        }
    )
    start = utc_now()
    for minute in range(5):
        # One breach per minute: never three inside a ten-second window.
        assert (
            engine.evaluate(
                a_fact(entity_id="e1", speed_kmh=30.0, ts=start + timedelta(minutes=minute))
            )
            == []
        )


def test_count_distinct_stops_one_person_looking_like_a_crowd() -> None:
    """Counting messages would let one person standing still under a 4 Hz camera register as a crowd —
    the sort of bug that produces confident nonsense."""
    engine = engine_from(
        {
            "id": "crowd",
            "emits": "crowd_gathering",
            "kinds": ["entity"],
            "when": [{"field": "entity_type", "op": "eq", "value": "person"}],
            "window": {
                "aggregate": "count_distinct",
                "of": "entity_id",
                "seconds": 60,
                "group_by": ["zone_id"],
                "op": "gte",
                "value": 4,
            },
            "cooldown_seconds": 180,
            "cooldown_key": ["zone_id"],
        }
    )
    start = utc_now()
    # Forty sightings of the same person.
    for step in range(40):
        assert (
            engine.evaluate(
                a_fact(
                    entity_type="person",
                    entity_id="p1",
                    zone_id="apron",
                    ts=start + timedelta(seconds=step * 0.25),
                )
            )
            == []
        )
    # Four different people.
    matches = []
    for index in range(4):
        matches += engine.evaluate(
            a_fact(
                entity_type="person",
                entity_id=f"q{index}",
                zone_id="apron",
                ts=start + timedelta(seconds=20),
            )
        )
    # Exactly one event: the threshold is crossed on the fourth person and the cooldown holds it there
    # while more arrive. Without the cooldown a growing crowd would emit an event per person.
    assert len(matches) == 1
    assert matches[0].aggregate_value == 4.0


def test_window_aggregates_compute_correctly() -> None:
    for aggregate, expected in (("max", 30.0), ("min", 10.0), ("mean", 20.0), ("sum", 60.0)):
        engine = engine_from(
            {
                "id": f"agg_{aggregate}",
                "emits": "speeding",
                "kinds": ["entity"],
                "window": {
                    "aggregate": aggregate,
                    "of": "speed_kmh",
                    "seconds": 60,
                    "group_by": ["entity_id"],
                    "op": "gte",
                    "value": expected,
                },
                "cooldown_seconds": 0,
            }
        )
        start = utc_now()
        results = []
        for index, speed in enumerate((10.0, 20.0, 30.0)):
            results += engine.evaluate(
                a_fact(entity_id="e1", speed_kmh=speed, ts=start + timedelta(seconds=index))
            )
        assert results, f"{aggregate} should have reached {expected}"
        assert results[-1].aggregate_value == pytest.approx(expected)


def test_a_value_aggregate_requires_a_field() -> None:
    with pytest.raises(ValueError, match="needs an 'of' field"):
        Rule.model_validate(
            {"id": "x", "emits": "speeding", "window": {"aggregate": "max", "seconds": 10}}
        )


# --------------------------------------------------------------- absence rules
def test_an_absence_rule_fires_when_a_subject_goes_quiet() -> None:
    """The rule shape people forget. A machine that stops reporting cannot be noticed by an engine that
    only reacts to messages it receives."""
    engine = engine_from(
        {
            "id": "machine_stopped",
            "emits": "machine_stopped",
            "kinds": ["observation"],
            "when": [{"field": "metric", "op": "eq", "value": "machine_state"}],
            "absence": {"after_seconds": 120, "group_by": ["source_id"], "requires_history": 3},
            "cooldown_seconds": 0,
        }
    )
    start = utc_now()
    for index in range(4):
        engine.evaluate(
            a_fact(
                "observation",
                source_id="iot-press-1",
                metric="machine_state",
                ts=start + timedelta(seconds=index * 10),
            )
        )

    assert engine.check_absences(start + timedelta(seconds=60)) == [], "not silent long enough"
    matches = engine.check_absences(start + timedelta(seconds=300))
    assert len(matches) == 1
    assert "iot-press-1" in matches[0].subject
    assert any("last reported" in reason for reason in matches[0].reasons)
    assert engine.check_absences(start + timedelta(seconds=400)) == [], (
        "and it fires once, not forever"
    )


def test_an_absence_rule_ignores_a_subject_with_no_history() -> None:
    """Otherwise every machine on site is "stopped" the moment the engine starts, which is how an
    absence rule gets muted permanently."""
    engine = engine_from(
        {
            "id": "machine_stopped",
            "emits": "machine_stopped",
            "kinds": ["observation"],
            "absence": {"after_seconds": 60, "group_by": ["source_id"], "requires_history": 5},
        }
    )
    start = utc_now()
    engine.evaluate(a_fact("observation", source_id="iot-new-1", ts=start))
    assert engine.check_absences(start + timedelta(seconds=600)) == []


def test_a_recovered_subject_can_fire_again() -> None:
    """A machine that stops, is fixed, and stops again is two events."""
    engine = engine_from(
        {
            "id": "machine_stopped",
            "emits": "machine_stopped",
            "kinds": ["observation"],
            "absence": {"after_seconds": 60, "group_by": ["source_id"], "requires_history": 2},
        }
    )
    start = utc_now()
    for index in range(3):
        engine.evaluate(a_fact("observation", source_id="m1", ts=start + timedelta(seconds=index)))
    assert len(engine.check_absences(start + timedelta(seconds=120))) == 1

    recovered = start + timedelta(seconds=200)
    for index in range(3):
        engine.evaluate(
            a_fact("observation", source_id="m1", ts=recovered + timedelta(seconds=index))
        )
    assert len(engine.check_absences(recovered + timedelta(seconds=120))) == 1


# ---------------------------------------------------------------------- upkeep
def test_hot_reload_preserves_window_state() -> None:
    """A reload that cleared the windows would blind every window rule for its whole span, so editing
    one rule would silently suppress crowd detection for a minute."""
    definition = {
        "id": "crowd",
        "emits": "crowd_gathering",
        "kinds": ["entity"],
        "window": {
            "aggregate": "count",
            "seconds": 60,
            "group_by": ["zone_id"],
            "op": "gte",
            "value": 3,
        },
        "cooldown_seconds": 0,
    }
    engine = engine_from(definition)
    start = utc_now()
    engine.evaluate(a_fact(zone_id="apron", ts=start))
    engine.evaluate(a_fact(zone_id="apron", ts=start + timedelta(seconds=1)))

    engine.replace_rules(RuleSet(rules=[Rule.model_validate(definition)]))
    matches = engine.evaluate(a_fact(zone_id="apron", ts=start + timedelta(seconds=2)))
    assert len(matches) == 1, "the two earlier facts must still be in the window"


def test_pruning_bounds_engine_state() -> None:
    """Without this the engine is a slow memory leak keyed by entity id, and ids are minted for every
    new object that appears."""
    engine = engine_from(
        {
            "id": "crowd",
            "emits": "crowd_gathering",
            "kinds": ["entity"],
            "window": {
                "aggregate": "count",
                "seconds": 60,
                "group_by": ["entity_id"],
                "op": "gte",
                "value": 99,
            },
            "cooldown_seconds": 0,
        }
    )
    start = utc_now()
    for index in range(200):
        engine.evaluate(a_fact(entity_id=f"ent_{index}", ts=start))
    assert len(engine._windows) == 200
    engine.prune(start + timedelta(hours=2))
    assert len(engine._windows) == 0


# --------------------------------------------------------------------- anomaly
def test_the_detector_learns_a_baseline_before_judging() -> None:
    """A detector that flags its first observation flags everything."""
    detector = RobustZScoreDetector(warmup=20)
    start = utc_now()
    for index in range(15):
        verdict = detector.observe(
            FeatureVector(ts=start + timedelta(minutes=index), values={"events_per_min": 10.0})
        )
        assert not verdict.is_anomaly
        assert "learning" in verdict.reasons[0]


def test_the_detector_flags_a_departure_and_names_the_feature() -> None:
    """UC6: notice something odd without being told what odd looks like, and say WHICH measurement was
    odd, because "anomaly score 0.87" is not actionable."""
    detector = RobustZScoreDetector(warmup=20, z_threshold=4.0)
    start = utc_now()
    for index in range(40):
        detector.observe(
            FeatureVector(
                ts=start + timedelta(minutes=index),
                values={"events_per_min": 10.0 + (index % 3), "mean_speed_kmh": 8.0},
            )
        )

    verdict = detector.observe(
        FeatureVector(
            ts=start + timedelta(minutes=41),
            values={"events_per_min": 140.0, "mean_speed_kmh": 8.0},
        )
    )
    assert verdict.is_anomaly
    assert verdict.top_features == ["events_per_min"], "and it names the feature that moved"
    assert 0 < verdict.score <= 1.0, "a score with no ceiling cannot be ranked"
    assert any("140" in reason for reason in verdict.reasons)
    assert all("mean_speed" not in reason for reason in verdict.reasons), (
        "the steady feature is not blamed"
    )


def test_robust_statistics_resist_masking() -> None:
    """Mean and standard deviation are both dragged toward an outlier inside the window, which lets a
    large anomaly hide itself. That failure is called masking, and it is the case this exists to catch.
    """
    detector = RobustZScoreDetector(warmup=20, z_threshold=4.0)
    start = utc_now()
    for index in range(30):
        detector.observe(FeatureVector(ts=start + timedelta(minutes=index), values={"x": 10.0}))
    # One huge spike enters the history...
    detector.observe(FeatureVector(ts=start + timedelta(minutes=31), values={"x": 5000.0}))
    # ...and a second, smaller anomaly must still be caught. A mean/stdev baseline would now have a
    # standard deviation of ~900 and would shrug at 60.
    verdict = detector.observe(FeatureVector(ts=start + timedelta(minutes=32), values={"x": 60.0}))
    assert verdict.is_anomaly, "the spike must not have raised the bar"


def test_a_constant_feature_does_not_make_every_change_infinite() -> None:
    """A window of identical values has a MAD of zero. Dividing by it would make a sensor reporting
    exactly 20.0 for an hour and then 20.1 infinitely anomalous."""
    detector = RobustZScoreDetector(warmup=10, z_threshold=4.0)
    start = utc_now()
    for index in range(30):
        detector.observe(FeatureVector(ts=start + timedelta(minutes=index), values={"x": 20.0}))
    verdict = detector.observe(FeatureVector(ts=start + timedelta(minutes=31), values={"x": 20.1}))
    assert not verdict.is_anomaly, "a 0.5% change is not an incident"
    big = detector.observe(FeatureVector(ts=start + timedelta(minutes=32), values={"x": 200.0}))
    assert big.is_anomaly, "but a tenfold change is"


def test_a_value_is_never_scored_against_a_baseline_it_helped_set() -> None:
    detector = RobustZScoreDetector(warmup=5, z_threshold=3.0)
    start = utc_now()
    for index in range(10):
        detector.observe(FeatureVector(ts=start + timedelta(minutes=index), values={"x": 5.0}))
    first = detector.observe(FeatureVector(ts=start + timedelta(minutes=11), values={"x": 500.0}))
    assert first.is_anomaly, "the outlier is judged against the history before it"


def test_build_detector_always_returns_something() -> None:
    """Requesting PyOD where it is not installed must degrade, not fail: the fallback is honest and the
    warning says which one is running."""
    assert build_detector("auto").name == "robust_zscore"
    detector = build_detector("pyod")
    assert detector.name in ("pyod_iforest", "robust_zscore")
    assert "detector" in detector.describe()


# ----------------------------------------------------------- engine reporting
def test_the_engine_reports_what_it_is_doing() -> None:
    """An operator asking "why did that not fire?" needs the suppression counts and the rule errors."""
    engine = RuleEngine(load_rules(RULES_DIR))
    description = engine.describe()
    assert description["enabled"] >= 12
    assert set(description["by_shape"]) == {"match", "window", "absence"}
    assert description["by_shape"]["absence"] >= 1, "at least one absence rule must ship"
    assert description["errors"] == []


# ------------------------------------------- regressions from the live firing counts
def test_the_shipped_speeding_rule_ignores_a_single_bad_fix() -> None:
    """The bug that 201 live firings exposed.

    The rule filtered its window to samples already above the limit and then tested their maximum —
    trivially true of anything that got in. It fired on single noisy fixes while its own explanation
    claimed it could not, and one event carried the note "aggregated over 1 facts in the window, so a
    single noisy sample cannot trigger this".
    """
    engine = RuleEngine(load_rules(RULES_DIR))
    start = utc_now()

    # A parked truck with one wild fix among steady slow ones.
    fired = []
    for index, speed in enumerate([8.0, 9.0, 45.0, 8.5, 9.5, 8.0, 9.0, 8.0]):
        fired += [
            match
            for match in engine.evaluate(
                a_fact(
                    entity_type="truck",
                    entity_id="ent_parked",
                    speed_kmh=speed,
                    ts=start + timedelta(seconds=index),
                )
            )
            if match.rule.id == "speeding"
        ]
    assert fired == [], "one 45 km/h fix among slow ones must not be speeding"


def test_the_shipped_speeding_rule_still_catches_a_vehicle_that_is_actually_speeding() -> None:
    """A rule that never fires is as useless as one that always does."""
    engine = RuleEngine(load_rules(RULES_DIR))
    start = utc_now()
    fired = []
    for index in range(8):
        fired += [
            match
            for match in engine.evaluate(
                a_fact(
                    entity_type="truck",
                    entity_id="ent_fast",
                    speed_kmh=32.0,
                    ts=start + timedelta(seconds=index),
                )
            )
            if match.rule.id == "speeding"
        ]
    assert len(fired) == 1, "sustained 32 km/h must fire exactly once under the cooldown"
    assert fired[0].aggregate_value == pytest.approx(32.0)


def test_no_shipped_window_rule_has_a_decorative_window() -> None:
    """A lint for the mistake above, so it cannot come back in another rule.

    If a window aggregates a field that ``when`` has already constrained with the same operator and
    threshold, the aggregate can only restate what admission already guaranteed — the window is
    decoration, and the rule fires on single samples while looking robust.
    """
    offenders = []
    for rule in load_rules(RULES_DIR).rules:
        if rule.window is None or rule.window.aggregate not in ("max", "min"):
            continue
        for condition in rule.when:
            if condition.field_path != rule.window.of:
                continue
            same_direction = (
                rule.window.aggregate == "max"
                and condition.op in ("gt", "gte")
                and rule.window.op in ("gt", "gte")
            ) or (
                rule.window.aggregate == "min"
                and condition.op in ("lt", "lte")
                and rule.window.op in ("lt", "lte")
            )
            if same_direction and _numeric_equal(condition.value, rule.window.value):
                offenders.append(
                    f"{rule.id}: when {condition.field_path} {condition.op} "
                    f"{condition.value} makes {rule.window.aggregate} "
                    f"{rule.window.op} {rule.window.value} a no-op"
                )
    assert offenders == [], "; ".join(offenders)


def _numeric_equal(left: object, right: object) -> bool:
    try:
        return float(left) == float(right)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return left == right


def test_min_samples_stops_an_aggregate_firing_on_a_thin_window() -> None:
    """A general guard, added because the speeding rule needed it twice over.

    Without a sample floor, every mean or max rule is wrong at the START of its window: an outlier
    arriving as the third sample of an intended ten is aggregated over three and fires.
    """
    engine = engine_from(
        {
            "id": "thin",
            "emits": "speeding",
            "kinds": ["entity"],
            "window": {
                "aggregate": "mean",
                "of": "speed_kmh",
                "seconds": 60,
                "group_by": ["entity_id"],
                "op": "gte",
                "value": 20,
                "min_samples": 5,
            },
            "cooldown_seconds": 0,
        }
    )
    start = utc_now()
    for index in range(4):
        assert (
            engine.evaluate(
                a_fact(entity_id="e1", speed_kmh=100.0, ts=start + timedelta(seconds=index))
            )
            == []
        )
    assert engine.stats["below_min_samples"] == 4
    matches = engine.evaluate(
        a_fact(entity_id="e1", speed_kmh=100.0, ts=start + timedelta(seconds=5))
    )
    assert len(matches) == 1, "the fifth sample completes the floor"


def test_the_median_aggregate_is_robust_to_one_outlier() -> None:
    """Robust by construction, rather than by hoping the window is long enough to dilute the outlier."""
    engine = engine_from(
        {
            "id": "median_speed",
            "emits": "speeding",
            "kinds": ["entity"],
            "window": {
                "aggregate": "median",
                "of": "speed_kmh",
                "seconds": 60,
                "group_by": ["entity_id"],
                "op": "gte",
                "value": 20,
                "min_samples": 5,
            },
            "cooldown_seconds": 0,
        }
    )
    start = utc_now()
    fired = []
    for index, speed in enumerate([8.0, 9.0, 120.0, 8.5, 9.0, 8.0]):
        fired += engine.evaluate(
            a_fact(entity_id="e1", speed_kmh=speed, ts=start + timedelta(seconds=index))
        )
    assert fired == [], (
        "a 120 km/h outlier among slow fixes must not move the median past the limit"
    )
