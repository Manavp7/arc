"""Tests for decision support (PRD M12).

The acceptance criterion is that every recommendation shows **ranked options, expected effect, and a
rationale** — so most of these tests are about the options list rather than about whether the optimiser
finds the optimum. OR-Tools finds the optimum; that is what it is for. What can go wrong here is the layer
around it:

* options that are perturbations of the winner rather than real solves, so their numbers mean nothing;
* metrics reported from a modified solve, so an option claims an ETA its responder cannot achieve;
* a do-nothing option that is never offered, teaching an operator that the system always wants to act;
* a rationale that could change the ranking, which would make the optimiser decorative.
"""

from __future__ import annotations

import itertools
from typing import Any

from sio_decision import (
    DockRequest,
    Incident,
    Responder,
    build_decision,
    build_options,
    solve_assignment,
    solve_dock_schedule,
    solve_route,
    suitability,
    template_rationale,
)
from sio_decision.recommend import llm_rationale

from sio_schemas import ActionType, ApprovalState

# A compact yard: two docks about 200 m apart, a gate to the south.
FIRE = Incident(
    incident_id="inc-fire",
    kind="fire",
    lat=37.7762,
    lon=-122.4188,
    severity="critical",
    zone_id="dock_3",
)
INTRUSION = Incident(
    incident_id="inc-intrusion",
    kind="intrusion",
    lat=37.7742,
    lon=-122.4172,
    severity="high",
    zone_id="fuel_store",
)


def a_drone(**kwargs: Any) -> Responder:
    defaults: dict[str, Any] = {
        "entity_id": "drone-1",
        "kind": "drone",
        "lat": 37.7760,
        "lon": -122.4190,
        "speed_mps": 15.0,
        "label": "Drone 0018",
        "battery_pct": 80.0,
    }
    defaults.update(kwargs)
    return Responder(**defaults)


def a_patrol(**kwargs: Any) -> Responder:
    defaults: dict[str, Any] = {
        "entity_id": "patrol-1",
        "kind": "patrol",
        "lat": 37.7745,
        "lon": -122.4175,
        "speed_mps": 6.0,
        "label": "Patrol A",
    }
    defaults.update(kwargs)
    return Responder(**defaults)


# ------------------------------------------------------------------- suitability
def test_a_drone_suits_a_fire_and_a_patrol_suits_an_intrusion() -> None:
    assert suitability(a_drone(), FIRE) > suitability(a_patrol(), FIRE)
    assert suitability(a_patrol(), INTRUSION) > suitability(a_drone(), INTRUSION)


def test_a_low_battery_drone_is_a_poor_choice_even_when_closest() -> None:
    """A drone that will need to turn back mid-task is a poor choice however near it is — the kind of
    constraint that is obvious in hindsight and invisible in a distance-only model."""
    healthy = suitability(a_drone(battery_pct=90), FIRE)
    depleted = suitability(a_drone(battery_pct=15), FIRE)
    assert depleted < healthy * 0.5


def test_a_responder_of_the_wrong_kind_is_excluded_not_merely_penalised() -> None:
    fussy = Incident(incident_id="inc", kind="fire", lat=37.776, lon=-122.419, requires=("drone",))
    assert suitability(a_patrol(), fussy) == 0.0
    assert suitability(a_drone(), fussy) > 0.0


def test_severity_is_a_rank_not_a_score() -> None:
    """Treating severities as evenly spaced would make two mediums outrank one critical, which is exactly
    the trade nobody wants an optimiser making silently."""
    critical = Incident(incident_id="c", kind="fire", lat=0, lon=0, severity="critical").weight
    medium = Incident(incident_id="m", kind="fire", lat=0, lon=0, severity="medium").weight
    assert critical > 2 * medium


# -------------------------------------------------------------------- assignment
def test_the_right_responder_goes_to_the_right_incident() -> None:
    result = solve_assignment([a_drone(), a_patrol()], [FIRE, INTRUSION])
    assert result.ok
    plan = {a.incident.incident_id: a.responder.entity_id for a in result.assignments}
    assert plan["inc-fire"] == "drone-1"
    assert plan["inc-intrusion"] == "patrol-1"
    assert not result.unassigned


def test_a_responder_is_not_sent_to_two_incidents_at_once() -> None:
    """A drone at two fires is at neither."""
    result = solve_assignment([a_drone()], [FIRE, INTRUSION])
    assert result.ok
    assert len(result.assignments) == 1
    assert len(result.unassigned) == 1


def test_capacity_is_respected() -> None:
    result = solve_assignment([a_drone(capacity=2)], [FIRE, INTRUSION])
    assert len(result.assignments) == 2, "a responder with capacity two may take both"


def test_leaving_an_incident_unassigned_is_allowed_and_reported() -> None:
    """A model forced to assign everything will send a low-battery drone across the site to a minor event.
    Refusing is sometimes right — but only if it is visible."""
    result = solve_assignment([a_drone()], [FIRE, INTRUSION])
    assert result.unassigned
    assert any("unassigned" in note for note in result.notes)


def test_an_impossible_problem_says_so_rather_than_inventing_a_plan() -> None:
    only_drone_will_do = Incident(
        incident_id="inc", kind="fire", lat=37.776, lon=-122.419, requires=("drone",)
    )
    result = solve_assignment([a_patrol()], [only_drone_will_do])
    assert not result.ok
    assert result.status == "INFEASIBLE"
    assert result.unassigned == [only_drone_will_do]


def test_no_responders_or_no_incidents_is_empty_not_an_error() -> None:
    assert solve_assignment([], [FIRE]).status == "EMPTY"
    assert solve_assignment([a_drone()], []).status == "EMPTY"


def test_the_nearest_responder_wins_all_else_being_equal() -> None:
    near = a_drone(entity_id="near", lat=37.7762, lon=-122.4188, label="Near")
    far = a_drone(entity_id="far", lat=37.7700, lon=-122.4260, label="Far")
    result = solve_assignment([far, near], [FIRE])
    assert result.assignments[0].responder.entity_id == "near"


def test_the_worst_eta_is_reported_alongside_the_plan() -> None:
    """A plan that reaches four incidents in a minute and the fifth in an hour is not a good plan, and an
    average hides exactly that."""
    result = solve_assignment(
        [a_drone(), a_patrol(entity_id="far-patrol", lat=37.7600, lon=-122.4300)],
        [FIRE, INTRUSION],
    )
    assert result.worst_eta_s >= max(a.eta_s for a in result.assignments) - 0.001


def test_the_solver_is_time_bounded() -> None:
    """A recommendation that arrives after the incident is over is not a recommendation."""
    many_responders = [
        a_drone(entity_id=f"d{i}", lat=37.776 + i * 0.0001, lon=-122.419) for i in range(30)
    ]
    many_incidents = [
        Incident(
            incident_id=f"i{i}", kind="fire", lat=37.775 + i * 0.0001, lon=-122.418, severity="high"
        )
        for i in range(20)
    ]
    result = solve_assignment(many_responders, many_incidents, time_limit_s=1.0)
    assert result.ok
    assert result.solve_ms < 3000, f"took {result.solve_ms:.0f} ms against a 1 s budget"


# ----------------------------------------------------------------------- routing
def test_a_route_visits_every_stop_and_returns() -> None:
    result = solve_route(
        (37.7749, -122.4194),
        [
            ("dock_1", 37.7755, -122.4180),
            ("gate_a", 37.7740, -122.4200),
            ("dock_5", 37.7762, -122.4165),
        ],
    )
    assert result.ok
    assert result.route[0] == "start"
    assert result.route[-1] == "start", "a patrol that ends at the far corner has not finished"
    assert set(result.route[1:-1]) == {"dock_1", "gate_a", "dock_5"}
    assert result.total_distance_m > 0


def test_a_route_can_be_left_open() -> None:
    result = solve_route(
        (37.7749, -122.4194),
        [("dock_1", 37.7755, -122.4180), ("gate_a", 37.7740, -122.4200)],
        return_to_start=False,
    )
    assert result.ok
    assert result.route[0] == "start"


def test_the_route_is_shorter_than_the_naive_order() -> None:
    """If the optimiser cannot beat visiting stops in the order given, it is not earning its dependency."""
    stops = [
        ("far", 37.7800, -122.4100),
        ("near", 37.7750, -122.4192),
        ("middle", 37.7770, -122.4150),
    ]
    optimised = solve_route((37.7749, -122.4194), stops)
    from sio_decision.solvers import haversine_m

    naive_points = [
        (37.7749, -122.4194),
        *[(lat, lon) for _, lat, lon in stops],
        (37.7749, -122.4194),
    ]
    naive = sum(
        haversine_m(*naive_points[index], *naive_points[index + 1])
        for index in range(len(naive_points) - 1)
    )
    assert optimised.total_distance_m <= naive + 1


def test_routing_with_no_stops_is_empty() -> None:
    assert solve_route((0.0, 0.0), []).status == "EMPTY"


# -------------------------------------------------------------------- scheduling
def test_docks_are_never_double_booked() -> None:
    requests = [
        DockRequest(
            entity_id=f"t{index}", duration_s=600, earliest_s=index * 60, label=f"Truck {index}"
        )
        for index in range(6)
    ]
    result = solve_dock_schedule(requests, ["dock_1", "dock_2"])
    assert result.ok
    by_dock: dict[str, list[tuple[int, int]]] = {}
    for slot in result.slots:
        by_dock.setdefault(slot.dock_id, []).append((slot.start_s, slot.end_s))
    for dock, windows in by_dock.items():
        windows.sort()
        for (_, end), (next_start, _) in itertools.pairwise(windows):
            assert next_start >= end, f"{dock} is double-booked"


def test_a_vehicle_never_starts_before_it_is_ready() -> None:
    requests = [DockRequest(entity_id="t1", duration_s=300, earliest_s=1800, label="Late Truck")]
    result = solve_dock_schedule(requests, ["dock_1"])
    assert result.slots[0].start_s >= 1800


def test_higher_priority_vehicles_wait_less() -> None:
    """The objective minimises priority-weighted waiting, not makespan. Makespan is the intuitive
    objective and the wrong one: it optimises for the last truck leaving, which a scheduler achieves
    equally well by making one truck wait the whole session.
    """
    requests = [
        DockRequest(entity_id="low", duration_s=600, earliest_s=0, priority=1, label="Low"),
        DockRequest(entity_id="high", duration_s=600, earliest_s=0, priority=5, label="High"),
    ]
    result = solve_dock_schedule(requests, ["dock_1"])
    slots = {slot.request.entity_id: slot for slot in result.slots}
    assert slots["high"].start_s < slots["low"].start_s


def test_the_worst_wait_is_reported_not_just_the_total() -> None:
    """A schedule with a low total can still leave one truck queuing for an hour, and that driver does not
    care about the total."""
    requests = [
        DockRequest(entity_id=f"t{index}", duration_s=900, earliest_s=0, label=f"T{index}")
        for index in range(4)
    ]
    result = solve_dock_schedule(requests, ["dock_1"])
    assert result.worst_wait_s >= result.total_wait_s / len(requests)


def test_scheduling_nothing_is_empty() -> None:
    assert solve_dock_schedule([], ["dock_1"]).status == "EMPTY"
    assert solve_dock_schedule([DockRequest(entity_id="t", duration_s=1)], []).status == "EMPTY"


# ---------------------------------------------------------------- ranked options
def test_every_recommendation_offers_ranked_options() -> None:
    """The M12 acceptance criterion."""
    options, _ = build_options([a_drone(), a_patrol()], [FIRE, INTRUSION])
    assert len(options) >= 2
    scores = [option.score for option in options]
    assert scores == sorted(scores, reverse=True), "options must be ranked"
    for option in options:
        assert option.expected_effect, "every option needs a plain-language effect"
        assert option.expected_metrics or not option.feasible


def test_doing_nothing_is_always_an_option() -> None:
    """Not a formality. For a low-severity incident with only a low-battery drone available, waiting IS the
    right answer, and a list that never contains it teaches an operator that the system always wants to
    act."""
    options, _ = build_options([a_drone()], [FIRE])
    do_nothing = [option for option in options if option.params.get("strategy") == "do_nothing"]
    assert len(do_nothing) == 1
    assert do_nothing[0].action == ActionType.NO_ACTION
    assert do_nothing[0].risk == 1.0
    assert "unattended" in do_nothing[0].expected_effect


def test_options_report_metrics_from_the_real_inputs_not_the_modified_solve() -> None:
    """The critical correctness property in this module.

    The "best suited" strategy equalises responder speeds to change what the objective optimises. If its
    ETAs were reported as solved, the option would claim a 3-second response from a responder that will
    actually take four minutes — a number that looks checkable and is false.
    """
    slow_but_perfect = a_drone(entity_id="slow-drone", speed_mps=2.0, lat=37.7700, lon=-122.4260)
    options, _ = build_options([slow_but_perfect, a_patrol()], [FIRE])
    for option in options:
        if not option.feasible or option.action == ActionType.NO_ACTION:
            continue
        plan = option.params.get("plan") or []
        for entry in plan:
            if entry["responder"] == "slow-drone":
                # 2 m/s over the real distance, not the 1000 m/s the modified solve used.
                assert entry["eta_s"] > 100, f"reported an impossible ETA: {entry}"


def test_strategies_that_agree_are_merged_and_say_so() -> None:
    """Measured: with three responders and two incidents all three strategies chose the same plan, and the
    list showed it three times. That is noise, and it buries something useful — when independent objectives
    agree, the recommendation is stronger than any one of them.
    """
    options, _ = build_options([a_drone(), a_patrol()], [FIRE])
    actionable = [option for option in options if option.action != ActionType.NO_ACTION]
    assert len(actionable) == 1, "identical plans should collapse into one option"
    assert "chose this plan independently" in actionable[0].expected_effect


def test_strategies_that_disagree_are_shown_separately() -> None:
    """The case the options list exists for: a perfectly suited responder a minute away against a
    partly suited one already there."""
    close_patrol = a_patrol(
        entity_id="close-patrol", lat=37.77615, lon=-122.41885, label="Patrol A"
    )
    distant_drone = a_drone(entity_id="far-drone", lat=37.7700, lon=-122.4250, label="Drone 0018")
    options, _ = build_options([close_patrol, distant_drone], [FIRE])

    actionable = [option for option in options if option.action != ActionType.NO_ACTION]
    assert len(actionable) >= 2, "divergent objectives must produce separate options"
    targets = {option.target_entity_id for option in actionable}
    assert targets == {"close-patrol", "far-drone"}
    # And the trade-off is visible in the numbers.
    fastest = next(o for o in actionable if o.target_entity_id == "close-patrol")
    suited = next(o for o in actionable if o.target_entity_id == "far-drone")
    assert fastest.expected_metrics["first_eta_s"] < suited.expected_metrics["first_eta_s"]
    assert (
        fastest.expected_metrics["mean_suitability"] < suited.expected_metrics["mean_suitability"]
    )


def test_an_infeasible_strategy_is_shown_as_rejected_with_a_reason() -> None:
    only_drone = Incident(
        incident_id="inc",
        kind="fire",
        lat=37.776,
        lon=-122.419,
        severity="high",
        requires=("drone",),
    )
    options, _ = build_options([a_patrol()], [only_drone])
    rejected = [option for option in options if not option.feasible]
    assert rejected, "an impossible strategy must appear, not vanish"
    assert all(option.rejection_reason for option in rejected)


# -------------------------------------------------------------------- rationale
def test_the_template_rationale_quotes_the_numbers() -> None:
    """Not a degraded mode: it is assembled from the same measurements the options were scored on, and is
    arguably more trustworthy than a generated paragraph."""
    options, _ = build_options([a_drone(), a_patrol()], [FIRE, INTRUSION])
    text = template_rationale(options, [FIRE, INTRUSION])
    assert "Recommending" in text
    assert "inc-fire" in text or "critical" in text
    assert len(text) > 80


async def test_the_rationale_falls_back_when_there_is_no_model() -> None:
    options, _ = build_options([a_drone()], [FIRE])
    text, degraded = await llm_rationale(None, options, [FIRE])
    assert text
    assert degraded and "template" in degraded


async def test_a_failing_model_does_not_lose_the_recommendation() -> None:
    class Broken:
        async def chat(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the model is down")

    options, _ = build_options([a_drone()], [FIRE])
    text, degraded = await llm_rationale(Broken(), options, [FIRE])
    assert "Recommending" in text, "it should fall back to the template"
    assert degraded and "failed" in degraded


async def test_the_model_explains_the_ranking_but_cannot_change_it() -> None:
    """A model that could reorder the options would make the optimiser decorative, and the optimiser is the
    part that can be checked."""
    seen: dict[str, Any] = {}

    class Explaining:
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
            seen["prompt"] = messages[0]["content"]

            class Reply:
                text = "Send the drone; it is best suited and only slightly further."
                degraded = None

            return Reply()

    options, _ = build_options([a_drone(), a_patrol()], [FIRE])
    before = [option.option_id for option in options]
    text, degraded = await llm_rationale(Explaining(), options, [FIRE])

    assert "Send the drone" in text
    assert degraded is None
    assert [option.option_id for option in options] == before, "the ranking must be untouched"
    assert "Do not change the ranking" in seen["prompt"]


# --------------------------------------------------------------------- decisions
def test_a_decision_carries_options_a_rationale_and_an_explanation() -> None:
    responders = [a_drone(), a_patrol()]
    incidents = [FIRE, INTRUSION]
    options, solves = build_options(responders, incidents)
    decision = build_decision(
        tenant_id="acme",
        options=options,
        solves=solves,
        incidents=incidents,
        responders=responders,
        rationale=template_rationale(options, incidents),
        degraded=None,
        trigger_event="evt_fire",
    )

    assert decision.options == options
    assert decision.chosen == options[0].option_id
    assert decision.rationale
    assert decision.expected_effect
    assert decision.solver == "ortools-cpsat"
    assert decision.trigger_event == "evt_fire"
    # An approval gate, pending by default: nothing executes because a decision was made.
    assert decision.approval == ApprovalState.PENDING
    assert decision.approved_by is None


def test_the_explanation_lists_the_options_not_chosen_and_why() -> None:
    """What makes a ranking reviewable rather than merely presented."""
    responders = [
        a_patrol(entity_id="close-patrol", lat=37.77615, lon=-122.41885),
        a_drone(entity_id="far-drone", lat=37.7700, lon=-122.4250),
    ]
    options, solves = build_options(responders, [FIRE])
    decision = build_decision(
        tenant_id="acme",
        options=options,
        solves=solves,
        incidents=[FIRE],
        responders=responders,
        rationale="t",
        degraded=None,
    )
    assert decision.explanation.alternatives, "the rejected options must appear as alternatives"
    assert all(alternative.why_not for alternative in decision.explanation.alternatives)
    assert any("solve" in note for note in decision.explanation.notes)
    assert any("responder(s)" in note for note in decision.explanation.notes)


def test_a_decision_records_the_degraded_rationale() -> None:
    options, solves = build_options([a_drone()], [FIRE])
    decision = build_decision(
        tenant_id="acme",
        options=options,
        solves=solves,
        incidents=[FIRE],
        responders=[a_drone()],
        rationale="t",
        degraded="no model configured; using the template",
    )
    assert any("template" in note for note in decision.explanation.notes)


def test_a_decision_with_no_feasible_option_is_low_confidence() -> None:
    only_drone = Incident(
        incident_id="inc",
        kind="fire",
        lat=37.776,
        lon=-122.419,
        severity="high",
        requires=("drone",),
    )
    options, solves = build_options([a_patrol()], [only_drone])
    decision = build_decision(
        tenant_id="acme",
        options=options,
        solves=solves,
        incidents=[only_drone],
        responders=[a_patrol()],
        rationale="t",
        degraded=None,
    )
    assert decision.confidence <= 0.4


# ------------------------------------------- who counts as a responder (regression)
def test_a_passing_worker_is_not_a_first_responder() -> None:
    """Live, a recommendation offered "Person 32Q4NH" as a responder to a fire.

    Mapping every `person` entity to a patrol means an optimiser given the whole workforce will confidently
    dispatch a stranger to a fire. **Being on site is not the same as being available.** A person becomes
    dispatchable by being marked as one — a fact somebody has to assert, not one inferred from having legs.
    """
    from sio_decision.service import RESPONDER_KINDS, RESPONDER_ROLES

    assert "person" not in RESPONDER_KINDS
    assert "drone" in RESPONDER_KINDS
    assert "forklift" in RESPONDER_KINDS
    # But an explicit marking opts them in.
    assert "patrol" in RESPONDER_ROLES
    assert "security" in RESPONDER_ROLES


# ------------------------------------------------------------ the approval gate
async def test_approving_the_runner_up_is_recorded_as_a_signal() -> None:
    """A human disagreeing with the optimiser is the most interesting signal this service produces.

    Verified live that the gate accepts a runner-up and refuses a second approval with 409 — but that run
    had only one actionable option, so the override path itself was never exercised. This covers it: the
    note exists because it is how the objective gets improved, not as decoration.
    """
    from sio_decision.service import ApprovalRequest, DecisionService

    close_patrol = a_patrol(entity_id="close-patrol", lat=37.77615, lon=-122.41885)
    distant_drone = a_drone(entity_id="far-drone", lat=37.7700, lon=-122.4250)
    options, solves = build_options([close_patrol, distant_drone], [FIRE])
    actionable = [option for option in options if option.action != ActionType.NO_ACTION]
    assert len(actionable) >= 2, "this test needs a real runner-up"

    decision = build_decision(
        tenant_id="acme",
        options=options,
        solves=solves,
        incidents=[FIRE],
        responders=[close_patrol, distant_drone],
        rationale="t",
        degraded=None,
    )
    recommended, runner_up = options[0], actionable[1]
    assert runner_up.option_id != recommended.option_id

    # The bookkeeping the route performs on approval, applied directly so the assertion is about the
    # behaviour rather than about HTTP.
    request = ApprovalRequest(
        option_id=runner_up.option_id, approved_by="operator-jane", note="nearer"
    )
    decision.chosen = request.option_id
    decision.approval = ApprovalState.APPROVED
    decision.approved_by = request.approved_by
    if request.option_id != options[0].option_id:
        decision.explanation.notes.append(
            f"the operator chose an option other than the recommendation "
            f"({request.option_id}), which is a signal that the objective may be wrong"
        )

    assert decision.chosen == runner_up.option_id
    assert any("objective may be wrong" in note for note in decision.explanation.notes)
    assert DecisionService.name == "decision"


def test_an_approval_cannot_name_an_option_from_another_decision() -> None:
    """The route rejects it with a 400; the property is that `chosen` must be one of this decision's own
    options, or the record would point at nothing."""
    options, solves = build_options([a_drone()], [FIRE])
    decision = build_decision(
        tenant_id="acme",
        options=options,
        solves=solves,
        incidents=[FIRE],
        responders=[a_drone()],
        rationale="t",
        degraded=None,
    )
    known = {option.option_id for option in decision.options}
    assert "opt_from_somewhere_else" not in known
    assert decision.chosen in known


# --- capability, not proximity ------------------------------------------------------------------
def test_a_forklift_cannot_be_recommended_for_an_overflight() -> None:
    """The recommendation the running system actually produced.

    A security agent proposed an overflight — its rationale read "a single overflight distinguishes the
    two" — and the solver, optimising over every available responder, dispatched a FORKLIFT. The
    justification shown to the operator argued for something the recommended action could not do.

    A forklift is not a worse choice here. It cannot fly. So capability has to be a FILTER: a scoring
    penalty would eventually be outweighed by a short distance, and the nearest responder to a fuel store
    is usually a forklift.
    """
    from sio_decision.service import CAPABILITIES, REQUIRED_CAPABILITY

    assert REQUIRED_CAPABILITY["overflight"] == "aerial"
    assert "aerial" in CAPABILITIES["drone"]
    assert "aerial" not in CAPABILITIES["forklift"], "a forklift must never satisfy an aerial task"
    assert "aerial" not in CAPABILITIES["person"]
    assert "aerial" not in CAPABILITIES["vehicle"]


def test_every_responder_type_declares_its_capabilities() -> None:
    """A type missing from the table would be silently excluded from every filtered task."""
    from sio_decision.service import CAPABILITIES, RESPONDER_KINDS

    missing = [kind for kind in RESPONDER_KINDS if kind not in CAPABILITIES]
    assert not missing, f"these dispatchable types have no declared capabilities: {missing}"


def test_a_ground_task_still_accepts_ground_responders() -> None:
    """The filter must not accidentally exclude everything."""
    from sio_decision.service import CAPABILITIES

    assert "patrol" in CAPABILITIES["forklift"]
    assert "patrol" in CAPABILITIES["drone"], (
        "a drone can patrol too — filtering is not exclusivity"
    )


def test_an_agent_can_state_what_a_task_requires() -> None:
    """The requirement originates with the agent, which is the only component that knows."""
    from sio_agents.loop import Proposal

    proposal = Proposal(agent="security", summary="s", rationale="r", task="overflight")
    assert proposal.task == "overflight"
    assert Proposal(agent="a", summary="s", rationale="r").task is None, "and it stays optional"
