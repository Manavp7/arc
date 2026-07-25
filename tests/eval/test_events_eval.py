"""Event precision and recall on labelled scenarios (PRD §16, Phase 8).

Feeds hand-labelled fact sequences through the **real** rule engine and the **real** shipped rules, and scores
what came out against what should have.

**Why both numbers, always.** Precision and recall trade against each other, and a rule set can be made perfect
at either one alone: threshold everything at 0.99 and precision hits 1.0 while the fire nobody detected burns;
fire on every detection and recall hits 1.0 while the operator turns off notifications by lunchtime. Quoting
one without the other is how a rule set gets tuned in the wrong direction with a number to justify it.

**The scenarios are labelled by hand and deliberately include near-misses.** A fixture of obvious positives
measures nothing — every rule set scores 1.0 on a flame at 0.95 confidence. The cases that discriminate are the
ones just under a threshold, in the wrong zone, or at the wrong time of day, and those are where a rule change
actually shows up.

**This scores the rules that ship**, loaded from `infra/rules/`, not a fixture copy. A rule set that is tested
in a copy is one where the copy gets updated and the shipped file does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.eval

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_TIME = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)


@dataclass
class Scenario:
    """One labelled situation: the facts, and the events that should come out."""

    id: str
    description: str
    #: `(kind, fields, zone_id, source_id)` per fact, in order.
    facts: list[tuple[str, dict[str, Any], str | None, str | None]]
    #: Event types a correct rule set emits. Anything else emitted is a false positive.
    expect: set[str] = field(default_factory=set)
    #: Why this case is here — printed on failure, because a scenario whose purpose is not obvious gets
    #: "fixed" by relaxing it the first time it fails.
    rationale: str = ""


SCENARIOS: tuple[Scenario, ...] = (
    # --- clear positives: the rule set must find these ------------------------------------------
    Scenario(
        id="obvious_fire",
        description="A flame detection well above the threshold",
        facts=[("detection", {"class": "fire", "confidence": 0.91}, "fuel_store", "cam-fuel")],
        expect={"fire_detected"},
        rationale="If this does not fire, the rule set is broken outright.",
    ),
    Scenario(
        id="smoke",
        description="A smoke detection",
        facts=[("detection", {"class": "smoke", "confidence": 0.72}, "dock_3", "cam-dock-3-4")],
        expect={"smoke_detected"},
        rationale="Smoke precedes fire; missing it costs the minutes that matter.",
    ),
    Scenario(
        id="hot_sensor",
        description="A temperature sensor well over its limit",
        facts=[
            (
                "observation",
                {"modality": "iot", "metric": "temperature_c", "value": 78.0},
                "fuel_store",
                "iot-temp-fuel",
            )
        ],
        expect={"temperature_spike"},
        rationale="A thermal reading is the corroboration a camera-only fire alert lacks.",
    ),
    # --- near-misses: the rule set must NOT fire on these ----------------------------------------
    Scenario(
        id="low_confidence_flame",
        description="A flame-like detection below the confidence floor",
        facts=[("detection", {"class": "fire", "confidence": 0.12}, "yard", "cam-yard-west")],
        expect=set(),
        rationale=(
            "The single most valuable negative in the fixture. A reflection off a windscreen is a "
            "low-confidence flame, and a rule set that fires here produces the alert fatigue that makes "
            "everything else it says worthless."
        ),
    ),
    Scenario(
        id="warm_but_normal",
        description="A temperature reading below the threshold",
        facts=[
            (
                "observation",
                {"modality": "iot", "metric": "temperature_c", "value": 31.0},
                "yard",
                "iot-temp-yard",
            )
        ],
        expect=set(),
        rationale="A warm afternoon is not an incident.",
    ),
    Scenario(
        id="ordinary_truck",
        description="A truck detected in a lane it is meant to be in",
        facts=[
            ("detection", {"class": "truck", "confidence": 0.88}, "lane_north", "cam-yard-east")
        ],
        expect=set(),
        rationale=(
            "The commonest fact on the site by a wide margin. Anything that fires here fires thousands "
            "of times a day."
        ),
    ),
    # --- the boundary: the case that actually discriminates ---------------------------------------
    Scenario(
        id="flame_just_under_threshold",
        description="A flame detection at 0.34, one hundredth below the rule's 0.35 floor",
        facts=[("detection", {"class": "fire", "confidence": 0.34}, "yard", "cam-yard-east")],
        expect=set(),
        rationale=(
            "The only scenario here that a plausible rule change actually moves. Everything else in this "
            "fixture is far from a threshold and scores 1.0 for any sane rule set, which measures nothing "
            "about the future — this one fails the moment somebody lowers the fire threshold to catch more, "
            "which is a change worth having to make deliberately."
        ),
    ),
    Scenario(
        id="flame_just_over_threshold",
        description="A flame detection at 0.36, one hundredth above the floor",
        facts=[("detection", {"class": "fire", "confidence": 0.36}, "yard", "cam-yard-east")],
        expect={"fire_detected"},
        rationale=(
            "The other side of the same boundary. A pair either side of a threshold is worth more than ten "
            "cases in the middle: together they pin the threshold's VALUE, not just its existence."
        ),
    ),
    Scenario(
        id="person_in_open_zone",
        description="A person walking in a non-restricted area",
        facts=[("detection", {"class": "person", "confidence": 0.83}, "yard", "cam-yard-west")],
        expect=set(),
        rationale="People in the yard are the normal state, not an intrusion.",
    ),
)


def _fact(kind: str, fields: dict[str, Any], zone: str | None, source: str | None, index: int):  # type: ignore[no-untyped-def]
    from sio_events.facts import Fact

    return Fact(
        kind=kind,
        ts=BASE_TIME + timedelta(seconds=index),
        fields=fields,
        zone_id=zone,
        source_id=source,
        tenant_id="default",
    )


def test_event_precision_and_recall(scorecard) -> None:  # type: ignore[no-untyped-def]
    """Score the shipped rules against the labelled scenarios."""
    precision_floor, recall_floor = 0.70, 0.70
    try:
        from sio_events.engine import RuleEngine
        from sio_events.rules import load_rules
    except ImportError as error:  # pragma: no cover
        scorecard.skip(
            "event precision", floor=precision_floor, reason=f"events unavailable: {error}"
        )
        scorecard.skip("event recall", floor=recall_floor, reason=f"events unavailable: {error}")
        pytest.skip("events is not installed")

    # The rules that SHIP, and without plugins: an installed plugin's rules would change the score depending on
    # what happens to be on the machine, which makes the number incomparable between runs.
    ruleset = load_rules(REPO_ROOT / "infra" / "rules", include_plugins=False)

    true_positives = 0
    false_positives = 0
    false_negatives = 0
    misfires: list[str] = []
    misses: list[str] = []

    for scenario in SCENARIOS:
        # A fresh engine per scenario. Sharing one would let a previous scenario's cooldown suppress a later
        # scenario's legitimate event, which scores the fixture's ORDER rather than the rules.
        engine = RuleEngine(ruleset)
        emitted: set[str] = set()
        for index, (kind, fields, zone, source) in enumerate(scenario.facts):
            for match in engine.evaluate(_fact(kind, fields, zone, source, index)):
                emitted.add(match.rule.emits)

        hits = emitted & scenario.expect
        true_positives += len(hits)
        extra = emitted - scenario.expect
        missing = scenario.expect - emitted
        false_positives += len(extra)
        false_negatives += len(missing)
        if extra:
            misfires.append(f"{scenario.id}: {sorted(extra)}")
        if missing:
            misses.append(f"{scenario.id}: {sorted(missing)}")

    precision = (
        true_positives / (true_positives + false_positives)
        if (true_positives + false_positives)
        else 1.0
    )
    recall = (
        true_positives / (true_positives + false_negatives)
        if (true_positives + false_negatives)
        else 1.0
    )

    positives = sum(1 for scenario in SCENARIOS if scenario.expect)
    negatives = len(SCENARIOS) - positives

    recorded_precision = scorecard.record(
        "event precision",
        precision,
        floor=precision_floor,
        detail=f"{negatives} near-miss scenarios; {false_positives} misfires",
    )
    recorded_recall = scorecard.record(
        "event recall",
        recall,
        floor=recall_floor,
        detail=f"{positives} positive scenarios; {false_negatives} missed",
    )

    assert recorded_precision.passed, (
        f"precision {precision:.2f} is below {precision_floor}. Rules fired on scenarios labelled as "
        f"non-events: {misfires}. Every one of these fires thousands of times a day on a real site, and alert "
        f"fatigue makes everything else the platform says worthless."
    )
    assert recorded_recall.passed, (
        f"recall {recall:.2f} is below {recall_floor}. Rules did not fire on scenarios that should have: "
        f"{misses}."
    )


def test_the_fixture_contains_near_misses() -> None:
    """A fixture of obvious positives measures nothing.

    Every rule set scores 1.0 on a flame at 0.95 confidence. The cases that discriminate are the ones just under
    a threshold or in the wrong place, and without them precision is unmeasurable — there is nothing to be
    imprecise about.
    """
    negatives = [scenario for scenario in SCENARIOS if not scenario.expect]
    assert len(negatives) >= 3, "precision needs scenarios where firing would be wrong"
    assert any("confidence" in str(scenario.facts) for scenario in negatives), (
        "at least one negative should sit just under a confidence threshold, which is where a rule change "
        "actually shows up"
    )


def test_every_scenario_says_why_it_exists() -> None:
    """A scenario whose purpose is not written down gets "fixed" by relaxing it the first time it fails."""
    for scenario in SCENARIOS:
        assert scenario.rationale, f"{scenario.id} has no rationale"


def test_the_shipped_rules_are_what_is_scored() -> None:
    """Not a fixture copy.

    A rule set tested against a copy is one where the copy gets updated and the shipped file does not — and the
    eval keeps reporting on rules nobody is running.
    """
    from sio_events.rules import load_rules

    ruleset = load_rules(REPO_ROOT / "infra" / "rules", include_plugins=False)
    assert len(ruleset.enabled()) >= 5, "the shipped rule set looks empty; check infra/rules"
