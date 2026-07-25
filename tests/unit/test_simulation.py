"""What-if simulation (PRD M11, Phase 6).

The plan's acceptance is precise: *"run a scenario, get quantified projected impact + affected entity list"*.
So the first thing every scenario test asserts is that there are **numbers and names**, because a projection
that returns a paragraph has not run.

The second theme is the counterfactual boundary. The platform already had a tool called `run_simulation` that
*injected a fire into the live simulated site* — which is the opposite of a projection. Asking "what if a gate
closed" and having a gate close is the difference between a forecast and an accident, so several tests here
exist to prove a scenario cannot reach anything live.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from sio_simulation.scenarios import (
    DOCK_SERVICE_S,
    DRONE_DRAIN_PCT_PER_MINUTE,
    SCENARIOS,
    FireSpread,
    Projection,
    Scenario,
)
from sio_simulation.world import SimEntity, SimZone, WorldSnapshot

BASE_LAT, BASE_LON = 37.7764, -122.4189


def rect(lat: float, lon: float, d: float = 0.0006) -> tuple[tuple[float, float], ...]:
    return ((lon - d, lat - d), (lon + d, lat - d), (lon + d, lat + d), (lon - d, lat + d))


def a_world(**overrides) -> WorldSnapshot:
    """A small but realistic yard: two gates, three docks, a fuel store, and nine things moving."""
    zones = [
        SimZone("gate_a", "Gate A", "gate", BASE_LAT, BASE_LON, polygon=rect(BASE_LAT, BASE_LON)),
        SimZone(
            "gate_b",
            "Gate B",
            "gate",
            BASE_LAT + 0.004,
            BASE_LON,
            polygon=rect(BASE_LAT + 0.004, BASE_LON),
        ),
        SimZone(
            "dock_1",
            "Dock 1",
            "dock",
            BASE_LAT + 0.001,
            BASE_LON + 0.001,
            polygon=rect(BASE_LAT + 0.001, BASE_LON + 0.001),
        ),
        SimZone(
            "dock_2",
            "Dock 2",
            "dock",
            BASE_LAT + 0.002,
            BASE_LON + 0.001,
            polygon=rect(BASE_LAT + 0.002, BASE_LON + 0.001),
        ),
        SimZone(
            "dock_3",
            "Dock 3",
            "dock",
            BASE_LAT + 0.003,
            BASE_LON + 0.001,
            polygon=rect(BASE_LAT + 0.003, BASE_LON + 0.001),
        ),
        SimZone(
            "fuel_store",
            "Fuel store",
            "area",
            BASE_LAT + 0.0015,
            BASE_LON + 0.002,
            restricted=True,
            polygon=rect(BASE_LAT + 0.0015, BASE_LON + 0.002),
        ),
        SimZone(
            "apron",
            "Apron",
            "area",
            BASE_LAT + 0.0018,
            BASE_LON + 0.0022,
            polygon=rect(BASE_LAT + 0.0018, BASE_LON + 0.0022),
        ),
    ]
    entities = [
        SimEntity("t1", "truck", "Truck A", BASE_LAT + 0.0002, BASE_LON, "gate_a"),
        SimEntity("t2", "truck", "Truck B", BASE_LAT + 0.0038, BASE_LON, "gate_b"),
        SimEntity("t3", "truck", "Truck C", BASE_LAT + 0.001, BASE_LON + 0.001, "dock_1"),
        SimEntity("t4", "truck", "Truck D", BASE_LAT + 0.003, BASE_LON + 0.001, "dock_3"),
        SimEntity("p1", "person", "Worker 1", BASE_LAT + 0.0016, BASE_LON + 0.002, "fuel_store"),
        SimEntity("p2", "person", "Worker 2", BASE_LAT + 0.0019, BASE_LON + 0.0022, "apron"),
        SimEntity("f1", "forklift", "Forklift 7", BASE_LAT + 0.0012, BASE_LON + 0.0012, "dock_1"),
        SimEntity(
            "d1", "drone", "Drone 18", BASE_LAT + 0.02, BASE_LON + 0.02, None, battery_pct=22.0
        ),
        SimEntity(
            "d2",
            "drone",
            "Drone 19",
            BASE_LAT + 0.0016,
            BASE_LON + 0.002,
            "fuel_store",
            battery_pct=95.0,
        ),
    ]
    return WorldSnapshot(
        taken_at=overrides.pop("taken_at", datetime.now(UTC)),
        entities=tuple(overrides.pop("entities", entities)),
        zones=tuple(overrides.pop("zones", zones)),
        open_alerts=overrides.pop("open_alerts", 3),
        events_last_hour=overrides.pop("events_last_hour", 40),
    )


# --- the acceptance criterion -------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_produces_numbers_and_names(name: str) -> None:
    """The plan's acceptance, applied to all six.

    A projection that returns a narrative and no figures has not run — an operator cannot compare "throughput
    would suffer" against anything, and cannot act on it either.
    """
    params = {
        "gate_closure": {"zone_id": "gate_a"},
        "dock_breakdown": {"zone_id": "dock_3"},
        "fire_spread": {"zone_id": "fuel_store", "wind_speed_mps": 5, "wind_bearing_deg": 180},
        "flood_level": {"level_m": 0.4},
        "drone_battery_death": {"base_zone_id": "apron"},
        "bridge_collapse": {"zone_id": "apron"},
    }[name]
    projection = SCENARIOS[name].project(a_world(), params)

    assert projection.summary, f"{name} produced no summary"
    assert projection.kpi_deltas, f"{name} produced no quantified impact"
    assert all(isinstance(value, (int, float)) for value in projection.kpi_deltas.values()), (
        f"{name} produced a non-numeric KPI"
    )
    assert 0.0 <= projection.confidence <= 1.0
    # Assumptions are not optional. Every constant in these projections was chosen rather than measured, and
    # a number shown without them invites an operator to treat a guess as a measurement.
    assert projection.assumptions, f"{name} states no assumptions"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_declares_its_parameters(name: str) -> None:
    """One schema, used by the API, the UI and the copilot tool.

    Three hand-written copies would drift, and the drift would present as a tool the model cannot call.
    """
    scenario = SCENARIOS[name]
    assert scenario.question, f"{name} does not say what question it answers"
    assert scenario.parameters.get("type") == "object"
    assert scenario.parameters.get("properties"), f"{name} declares no parameters"


# --- the counterfactual boundary ----------------------------------------------------------------
def test_a_scenario_cannot_reach_anything_live() -> None:
    """Enforced by the types, not by discipline.

    A `Scenario` is handed a frozen `WorldSnapshot` and nothing else — no client, no pool, no bus. That is
    what stops a what-if becoming a what-happened, and it is worth a test because the platform already had a
    `run_simulation` tool that injected a real fire.
    """
    import inspect

    signature = inspect.signature(Scenario.project)
    assert list(signature.parameters) == ["self", "world", "params"], (
        "a scenario takes only a snapshot and parameters; anything else is a route to the live site"
    )
    assert dataclasses.is_dataclass(WorldSnapshot)
    assert WorldSnapshot.__dataclass_params__.frozen, "the snapshot must be immutable"


def test_running_a_scenario_does_not_modify_the_snapshot() -> None:
    """Two runs against one world must produce identical results.

    If a scenario could mutate its input, the second run of a comparison would silently be measuring
    something different from the first — which is exactly the kind of bug that makes a projection look
    unstable and gets the whole feature distrusted.
    """
    world = a_world()
    before = world.describe()
    first = SCENARIOS["dock_breakdown"].project(world, {"zone_id": "dock_3"})
    second = SCENARIOS["dock_breakdown"].project(world, {"zone_id": "dock_3"})
    assert world.describe() == before
    assert first.kpi_deltas == second.kpi_deltas
    assert first.summary == second.summary


# --- the edge cases a naive model gets wrong -----------------------------------------------------
def test_closing_the_only_gate_stops_the_site_rather_than_halving_throughput() -> None:
    """The case a naive model divides by zero on.

    Redistributing traffic across "the remaining gates" is meaningless when there are none, and the honest
    answer is not a smaller number — it is that nothing moves.
    """
    one_gate = [zone for zone in a_world().zones if zone.zone_id != "gate_b"]
    world = a_world(zones=one_gate)
    projection = SCENARIOS["gate_closure"].project(world, {"zone_id": "gate_a"})
    assert "only gate" in projection.summary
    assert projection.kpi_deltas["trucks_blocked"] > 0
    assert projection.confidence >= 0.75, "this case is more certain, not less"
    assert any("only gate" in text for text in projection.recommendations)


def test_breaking_the_only_dock_halts_loading() -> None:
    docks = {"dock_2", "dock_3"}
    world = a_world(zones=[zone for zone in a_world().zones if zone.zone_id not in docks])
    projection = SCENARIOS["dock_breakdown"].project(world, {"zone_id": "dock_1"})
    assert "only dock" in projection.summary
    assert projection.kpi_deltas["docks_available"] == -1.0


def test_an_unknown_zone_is_refused_rather_than_projected() -> None:
    """A projection about a place that does not exist is worse than a refusal.

    It would return plausible numbers for nothing, and confidence 0 is the honest value. The refusal is still
    QUANTIFIED and still names the real zones, so the caller can retry rather than guess.
    """
    for name, key in (("fire_spread", "zone_id"), ("bridge_collapse", "zone_id")):
        projection = SCENARIOS[name].project(a_world(), {key: "the_roof"})
        assert "no zone" in projection.summary.lower()
        assert projection.confidence == 0.0
        assert projection.kpi_deltas, f"{name} refused without numbers"
        assert any("does not exist" in text for text in projection.assumptions)
        assert any("gate_a" in text for text in projection.assumptions), (
            "a refusal must name the zones that do exist, or the caller can only guess again"
        )


def test_no_drones_means_no_drones_can_die() -> None:
    """A trivially true answer stated with high confidence, rather than an empty result."""
    world = a_world(entities=[e for e in a_world().entities if e.type != "drone"])
    projection = SCENARIOS["drone_battery_death"].project(world, {"base_zone_id": "apron"})
    assert "no drones" in projection.summary.lower()
    assert projection.confidence == 1.0
    assert projection.kpi_deltas["drones_total"] == 0.0


# --- fire spread --------------------------------------------------------------------------------
def test_wind_makes_fire_reach_further_downwind() -> None:
    """The whole reason `fire_spread` is a cellular model rather than a radius."""
    world = a_world()
    still = SCENARIOS["fire_spread"].project(
        world, {"zone_id": "fuel_store", "duration_s": 1800, "wind_speed_mps": 0}
    )
    windy = SCENARIOS["fire_spread"].project(
        world,
        {"zone_id": "fuel_store", "duration_s": 1800, "wind_speed_mps": 10, "wind_bearing_deg": 0},
    )
    assert windy.kpi_deltas["downwind_reach_m"] > still.kpi_deltas["downwind_reach_m"]
    assert len(windy.impacted_entities) >= len(still.impacted_entities)


def test_fire_reaches_further_over_a_longer_horizon() -> None:
    world = a_world()
    short = SCENARIOS["fire_spread"].project(world, {"zone_id": "fuel_store", "duration_s": 300})
    long = SCENARIOS["fire_spread"].project(world, {"zone_id": "fuel_store", "duration_s": 3600})
    assert long.kpi_deltas["downwind_reach_m"] > short.kpi_deltas["downwind_reach_m"]
    assert len(long.impacted_entities) >= len(short.impacted_entities)


def test_people_are_named_first_in_a_fire_projection() -> None:
    """A projection that lists an evacuation and a gate closure in arbitrary order has buried the only
    recommendation that matters."""
    projection = SCENARIOS["fire_spread"].project(
        a_world(), {"zone_id": "fuel_store", "duration_s": 1800}
    )
    assert projection.kpi_deltas["people_at_risk"] >= 1
    assert projection.recommendations
    assert "Evacuate" in projection.recommendations[0]


def test_the_fire_timeline_is_ordered_by_arrival() -> None:
    """An unordered timeline is not a timeline."""
    projection = SCENARIOS["fire_spread"].project(
        a_world(), {"zone_id": "fuel_store", "duration_s": 3600}
    )
    times = [step["at_s"] for step in projection.timeline]
    assert times == sorted(times)


# --- provenance of the one grounded constant ----------------------------------------------------
def test_the_drone_drain_rate_matches_the_simulator() -> None:
    """The one constant here with real provenance, and a test so the claim stays true.

    A projection about the simulated site should use the simulated site's physics: a what-if computed with a
    different drain rate than the thing it is projecting is not a projection of anything.

    I originally wrote 1.2 and a comment claiming it came from the simulator. It did not — the simulator uses
    1.6. This test reads the simulator's own default, so the number cannot drift away from its source again,
    which is what makes the provenance claim true rather than aspirational.
    """
    from sio_ingest.sim.agents import Drone

    default = next(
        field.default for field in dataclasses.fields(Drone) if field.name == "drain_per_minute"
    )
    assert default == DRONE_DRAIN_PCT_PER_MINUTE, (
        f"the projection uses {DRONE_DRAIN_PCT_PER_MINUTE}%/min but the simulator drains {default}%/min"
    )


def test_a_drone_too_far_from_base_is_flagged() -> None:
    """Drone 18 is deliberately 2 km out with 22 % charge."""
    projection = SCENARIOS["drone_battery_death"].project(
        a_world(), {"base_zone_id": "apron", "reserve_pct": 20}
    )
    assert projection.kpi_deltas["drones_at_risk"] == 1.0
    assert "d1" in projection.impacted_entities
    at_risk = projection.detail["at_risk"][0]
    assert at_risk["short_by_pct"] > 0
    assert at_risk["distance_m"] > 1000


# --- geometry ------------------------------------------------------------------------------------
def test_longitude_is_scaled_by_latitude() -> None:
    """Omitting the cosine correction stretches every east-west distance.

    At 51° north a degree of longitude is 620 m shorter than a degree of latitude, so leaving the correction
    out inflates east-west distances by 60 % — which would silently bias every "nearest responder" answer in
    a yard laid out east to west.
    """
    north = SimEntity("n", "truck", None, 51.0, 0.0, None)
    # One degree east at 51°N is about 70 km, not 111 km.
    east = north.distance_to(51.0, 1.0)
    assert 68_000 < east < 72_000, f"east-west distance is {east:.0f} m; the cosine term is missing"
    # One degree north is the full 111 km wherever you are.
    assert 110_000 < north.distance_to(52.0, 0.0) < 112_000


def test_a_zone_contains_points_inside_its_polygon() -> None:
    zone = SimZone("z", "Z", "area", BASE_LAT, BASE_LON, polygon=rect(BASE_LAT, BASE_LON))
    assert zone.contains(BASE_LAT, BASE_LON)
    assert not zone.contains(BASE_LAT + 0.01, BASE_LON)


def test_a_zone_with_no_polygon_contains_nothing() -> None:
    """Rather than everything, which is what a missing-geometry bug usually produces."""
    zone = SimZone("z", "Z", "area", BASE_LAT, BASE_LON)
    assert not zone.contains(BASE_LAT, BASE_LON)


def test_zone_membership_uses_both_the_record_and_the_geometry() -> None:
    """They disagree, and each is right sometimes.

    Recorded membership comes from the spatial service's hysteresis, which deliberately lags to avoid event
    storms. Geometry is instantaneous. For a projection, an entity geometrically inside a zone that is about
    to catch fire is affected whether or not the debouncer has caught up.
    """
    world = a_world()
    # p1 is recorded in fuel_store; f1 is recorded in dock_1 but geometrically inside dock_1's polygon too.
    assert "p1" in {entity.entity_id for entity in world.in_zone("fuel_store")}
    inside_by_geometry = SimEntity("g1", "person", None, BASE_LAT + 0.0015, BASE_LON + 0.002, None)
    world = a_world(entities=[*a_world().entities, inside_by_geometry])
    assert "g1" in {entity.entity_id for entity in world.in_zone("fuel_store")}


# --- the projection contract --------------------------------------------------------------------
def test_a_projection_records_its_own_steps_in_order() -> None:
    projection = Projection(summary="s")
    projection.add_step(10, "second")
    projection.add_step(1, "first")
    assert [step["what"] for step in projection.timeline] == ["second", "first"], (
        "add_step appends; ordering is the scenario's responsibility and is tested per scenario"
    )


def test_dock_throughput_follows_from_the_service_time() -> None:
    """The arithmetic an operator should be able to check by hand.

    Three docks at fifteen minutes each is twelve movements an hour. If that is not obvious from the
    constants, the projection is not legible.
    """
    from sio_simulation.scenarios import _throughput_per_hour

    assert _throughput_per_hour(3) == pytest.approx(3600 / DOCK_SERVICE_S * 3)
    assert _throughput_per_hour(3) == pytest.approx(12.0)
    assert _throughput_per_hour(0) == 0.0


def test_fire_spread_is_the_only_scenario_with_spatial_dynamics() -> None:
    """Documents the SimPy/Mesa decision as an assertion rather than only as a comment.

    The PRD names both frameworks. Neither is used: the queueing scenarios are closed-form arithmetic over a
    handful of trucks, and wrapping that in a generator-based event loop would obscure numbers an operator
    needs to be able to check. `fire_spread` is the one scenario with real spatial dynamics, and the PRD's own
    description names the right tool for it — a cellular model, which needs no framework.
    """
    import sys

    assert "simpy" not in sys.modules
    assert "mesa" not in sys.modules
    assert issubclass(FireSpread, Scenario)
    # And the wind parameter is what makes it spatial rather than a radius.
    assert "wind_bearing_deg" in FireSpread.parameters["properties"]
