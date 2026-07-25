"""What-if scenarios (PRD M11, Phase 6).

Six scenarios, each answering one question with numbers and a named list of affected entities. The plan's
acceptance is exactly that: *"run a scenario, get quantified projected impact + affected entity list"* — so a
scenario that returns a narrative and no figures has not run.

**On SimPy and Mesa.** The PRD names both. Neither is used, and the reasoning is the same one that kept
PyTorch out of the perception stack.

SimPy is a discrete-event framework whose value is managing a simulation clock across many interacting
processes. The queueing scenarios here — a dock going down, a gate closing — are single-server queues over a
handful of trucks, where the projection is a closed-form arithmetic result plus a short deterministic loop.
Wrapping that in a generator-based event loop would add a dependency, obscure the arithmetic, and make the
numbers harder to check by hand. Mesa is agent-based and heavier still, for scenarios with at most a few
dozen agents and no emergent behaviour worth discovering.

`fire_spread` is the one scenario with genuinely spatial dynamics, and the PRD's own description names the
right tool: *"cellular spread over site grid + wind"* — a cellular automaton, which is thirty lines and no
framework.

The honest summary: these projections are **deliberately simple and legible**, because a number an operator
cannot sanity-check is a number they should not act on. Every scenario states its assumptions in the
explanation, and every one of them is wrong in ways the explanation names.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .world import SimEntity, WorldSnapshot

#: How fast fire crosses a metre of yard, per second, with no wind.
#:
#: 0.05 m/s is a slow ground fire over mixed surfaces. It is a guess, it is stated as one in every
#: explanation, and it is a single constant so that recalibrating against a real incident is a one-line change
#: rather than an archaeology exercise.
FIRE_SPREAD_MPS = 0.05

#: Multiplier on spread rate directly downwind. Fire runs with the wind; 3x at the stated wind speed is
#: conservative for a yard and, again, is one constant.
WIND_FACTOR = 3.0

#: Seconds a truck occupies a dock. Drives every queueing projection.
DOCK_SERVICE_S = 900.0

#: Metres per second a ground responder covers, and a drone.
GROUND_SPEED_MPS = 6.0
DRONE_SPEED_MPS = 15.0

#: Battery percent a drone consumes per minute of flight.
#:
#: The one constant here with real provenance: it is the yard simulator's own `PatrolDrone.drain_per_minute`.
#: That matters, because a projection about the simulated site should use the simulated site's physics — a
#: what-if computed with a different drain rate than the thing it is projecting is not a projection of
#: anything.
#:
#: I had written 1.2 and a comment claiming it came from the simulator. It did not; the simulator uses 1.6.
#: `test_simulation.py` now reads the simulator's value and asserts they agree, so the number cannot drift
#: away from its source again — which is the only thing that makes the provenance claim true rather than
#: aspirational.
DRONE_DRAIN_PCT_PER_MINUTE = 1.6


@dataclass
class Projection:
    """The result of a scenario: numbers, names, a sequence, and its own assumptions.

    `assumptions` is not decoration. Each of these projections rests on constants that were chosen rather
    than measured, and a projection that presents its output without them invites an operator to treat a guess
    as a measurement. It is rendered into the explanation and shown in the UI.
    """

    summary: str
    kpi_deltas: dict[str, float] = field(default_factory=dict)
    impacted_entities: list[str] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    confidence: float = 0.5
    recommendations: list[str] = field(default_factory=list)

    def add_step(self, at_s: float, what: str, **extra: Any) -> None:
        self.timeline.append({"at_s": round(at_s, 1), "what": what, **extra})


class Scenario(ABC):
    """One question about a world that does not exist.

    A scenario receives a frozen `WorldSnapshot` and returns a `Projection`. It has no client, no pool and no
    bus — not by convention but because it is never handed one, so there is no way for a what-if to become a
    what-happened.
    """

    name: str = ""
    question: str = ""
    #: Parameters, as JSON Schema, so the API and the copilot tool describe themselves from one source.
    parameters: ClassVar[dict[str, Any]] = {}

    @abstractmethod
    def project(self, world: WorldSnapshot, params: dict[str, Any]) -> Projection: ...

    # -- shared helpers ------------------------------------------------------------------
    @staticmethod
    def responders(world: WorldSnapshot) -> tuple[SimEntity, ...]:
        return world.of_type("drone", "forklift", "vehicle")

    @staticmethod
    def eta_s(entity: SimEntity, lat: float, lon: float) -> float:
        """Straight-line ETA. Optimistic, and the explanation says so.

        A yard has obstacles and a ground vehicle does not travel in straight lines, so every ground ETA here
        is a lower bound. Stating that is more useful than applying a fudge factor nobody can justify — an
        operator who knows a number is a floor can reason about it; one given a "corrected" number cannot.
        """
        speed = DRONE_SPEED_MPS if entity.is_airborne else GROUND_SPEED_MPS
        return entity.distance_to(lat, lon) / max(speed, 0.1)


class GateClosure(Scenario):
    """What happens to the yard if a gate closes?"""

    name = "gate_closure"
    question = "If a gate closes, what queues and how much throughput is lost?"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "zone_id": {"type": "string", "description": "The gate to close, e.g. gate_a"},
            "duration_s": {"type": "number", "description": "How long it stays shut"},
        },
        "required": ["zone_id"],
    }

    def project(self, world: WorldSnapshot, params: dict[str, Any]) -> Projection:
        gate_id = str(params.get("zone_id") or "gate_a")
        duration_s = float(params.get("duration_s") or 1800)
        gate = world.zone(gate_id)

        gates = [zone for zone in world.zones if zone.is_gate]
        remaining = [zone for zone in gates if zone.zone_id != gate_id]

        # Trucks that would have used this gate. Approximated as those nearest to it, because the platform
        # does not record intent — a truck's destination is not observable, only its position.
        trucks = world.of_type("truck")
        affected = [
            truck
            for truck in trucks
            if gate is not None
            and truck.distance_to(gate.lat, gate.lon)
            == min(
                (truck.distance_to(other.lat, other.lon) for other in gates),
                default=float("inf"),
            )
        ]

        projection = Projection(
            summary="",
            impacted_entities=[truck.entity_id for truck in affected],
            confidence=0.55,
        )
        projection.assumptions = [
            f"a truck uses its nearest gate, because the platform does not observe intent "
            f"({len(affected)} of {len(trucks)} trucks are nearest to {gate_id})",
            f"a dock visit takes {DOCK_SERVICE_S / 60:.0f} min",
            "traffic redistributes evenly across the remaining gates",
        ]

        if not remaining:
            # The interesting case, and the one a naive model gets wrong by dividing by zero. Closing the only
            # gate does not halve throughput — it stops the site.
            projection.summary = (
                f"{gate_id} is the only gate on this site. Closing it stops all arrivals and departures: "
                f"{len(affected)} truck(s) would be unable to move, and throughput falls to zero for "
                f"{duration_s / 60:.0f} min."
            )
            projection.kpi_deltas = {
                "throughput_per_h": -_throughput_per_hour(len(world.docks())),
                "gates_available": -1.0,
                "trucks_blocked": float(len(affected)),
            }
            projection.confidence = 0.8
            projection.recommendations = [
                "Do not close the only gate while trucks are on site",
                "Stage arrivals outside the perimeter before any single-gate closure",
            ]
            projection.add_step(0, f"{gate_id} closes; no alternative route exists")
            projection.add_step(duration_s, f"{gate_id} reopens; {len(affected)} truck(s) resume")
            return projection

        # Queueing: the redirected trucks join the remaining gates. Extra wait is the redirected arrivals
        # divided across the surviving gates, times the service time.
        per_gate = len(affected) / len(remaining)
        extra_wait_s = per_gate * (DOCK_SERVICE_S / max(len(world.docks()), 1))
        lost_throughput = _throughput_per_hour(len(world.docks())) * (
            1 - len(remaining) / max(len(gates), 1)
        )

        projection.summary = (
            f"Closing {gate_id} redirects {len(affected)} truck(s) to {len(remaining)} remaining "
            f"gate(s), adding about {extra_wait_s / 60:.1f} min to each affected arrival and costing "
            f"roughly {lost_throughput:.1f} movements/hour for {duration_s / 60:.0f} min."
        )
        projection.kpi_deltas = {
            "throughput_per_h": -round(lost_throughput, 2),
            "mean_wait_s": round(extra_wait_s, 1),
            "gates_available": -1.0,
            "trucks_redirected": float(len(affected)),
        }
        projection.recommendations = [
            f"Redirect to {', '.join(zone.zone_id for zone in remaining[:2])}",
            "Hold non-urgent arrivals until the gate reopens",
        ]
        projection.add_step(0, f"{gate_id} closes")
        projection.add_step(60, f"{len(affected)} truck(s) rerouted to {len(remaining)} gate(s)")
        projection.add_step(extra_wait_s, "queues at the remaining gates peak")
        projection.add_step(duration_s, f"{gate_id} reopens")
        return projection


class DockBreakdown(Scenario):
    """What happens to the queue if a dock goes down?"""

    name = "dock_breakdown"
    question = "If a dock breaks down, how long does the queue get?"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "zone_id": {"type": "string", "description": "The dock that fails, e.g. dock_3"},
            "duration_s": {"type": "number", "description": "Repair time in seconds"},
        },
        "required": ["zone_id"],
    }

    def project(self, world: WorldSnapshot, params: dict[str, Any]) -> Projection:
        dock_id = str(params.get("zone_id") or "dock_3")
        duration_s = float(params.get("duration_s") or 3600)
        docks = world.docks()
        surviving = [zone for zone in docks if zone.zone_id != dock_id]

        at_dock = world.in_zone(dock_id)
        waiting = [
            truck
            for truck in world.of_type("truck")
            if truck.zone_id not in {d.zone_id for d in docks}
        ]

        projection = Projection(
            summary="",
            impacted_entities=[item.entity_id for item in at_dock]
            + [truck.entity_id for truck in waiting],
            confidence=0.6,
        )
        projection.assumptions = [
            f"a dock visit takes {DOCK_SERVICE_S / 60:.0f} min",
            "trucks not currently at a dock are waiting for one",
            "the queue is served first-come, first-served with no priority",
        ]

        if not surviving:
            projection.summary = (
                f"{dock_id} is the only dock. Its failure halts all loading for "
                f"{duration_s / 60:.0f} min: {len(waiting)} truck(s) queue with no service."
            )
            projection.kpi_deltas = {
                "throughput_per_h": -_throughput_per_hour(1),
                "queue_length": float(len(waiting)),
                "docks_available": -1.0,
            }
            projection.confidence = 0.85
            projection.recommendations = ["Divert arrivals off site until the dock is repaired"]
            projection.add_step(0, f"{dock_id} fails; no alternative dock")
            projection.add_step(duration_s, "repaired")
            return projection

        # Little's law, in the form that matters here: with one fewer server, the same arrival rate produces
        # a longer queue. Arrival rate is estimated from what is on site rather than from history, which is
        # the honest limitation — a projection from a snapshot cannot know the arrival pattern.
        before = _throughput_per_hour(len(docks))
        after = _throughput_per_hour(len(surviving))
        backlog = len(waiting) + len(at_dock)
        drain_s = backlog * DOCK_SERVICE_S / max(len(surviving), 1)

        projection.summary = (
            f"With {dock_id} down, {len(surviving)} dock(s) remain. Capacity falls from "
            f"{before:.1f} to {after:.1f} movements/hour, and the {backlog} truck(s) on site would take "
            f"about {drain_s / 60:.0f} min to clear — {len(at_dock)} of them must move off {dock_id} first."
        )
        projection.kpi_deltas = {
            "throughput_per_h": -round(before - after, 2),
            "queue_length": float(backlog),
            "drain_time_s": round(drain_s, 0),
            "docks_available": -1.0,
        }
        projection.recommendations = [
            f"Move {len(at_dock)} truck(s) off {dock_id}"
            if at_dock
            else "The dock is already clear",
            f"Reassign arrivals to {', '.join(zone.zone_id for zone in surviving[:3])}",
        ]
        projection.add_step(0, f"{dock_id} fails", at_dock=len(at_dock))
        projection.add_step(300, f"{len(at_dock)} truck(s) relocated")
        projection.add_step(drain_s, f"backlog of {backlog} cleared")
        projection.add_step(duration_s, f"{dock_id} back in service")
        return projection


class FireSpread(Scenario):
    """Where does a fire reach, and who is in its way?

    The one scenario with genuinely spatial dynamics, and the PRD names the right tool for it: a cellular
    automaton over the site with a wind bias. No framework — a grid, a front, and a loop.
    """

    name = "fire_spread"
    question = "If a fire starts here, where does it reach and who is affected?"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "zone_id": {"type": "string", "description": "Where it starts"},
            "duration_s": {"type": "number", "description": "How far ahead to project"},
            "wind_bearing_deg": {
                "type": "number",
                "description": "Direction the wind blows TOWARDS, degrees clockwise from north",
            },
            "wind_speed_mps": {"type": "number"},
        },
        "required": ["zone_id"],
    }

    def project(self, world: WorldSnapshot, params: dict[str, Any]) -> Projection:
        origin_id = str(params.get("zone_id") or "fuel_store")
        horizon_s = float(params.get("duration_s") or 1800)
        bearing = float(params.get("wind_bearing_deg") or 0.0)
        wind_mps = float(params.get("wind_speed_mps") or 0.0)
        origin = world.zone(origin_id)

        projection = Projection(summary="", confidence=0.4)
        if origin is None:
            # A refusal, still quantified, and it names the zones that DO exist.
            #
            # My own acceptance test caught the first version returning an empty `kpi_deltas`: a projection
            # with no numbers has not run, even when the honest answer is zero. And a refusal that does not
            # list the real zones leaves the caller — often a language model — to guess again.
            projection.summary = f"There is no zone called {origin_id!r} on this site, so a fire there cannot be projected."
            projection.confidence = 0.0
            projection.kpi_deltas = {
                "entities_at_risk": 0.0,
                "people_at_risk": 0.0,
                "zones_at_risk": 0.0,
            }
            projection.assumptions = [
                f"{origin_id!r} does not exist; the known zones are "
                f"{', '.join(zone.zone_id for zone in world.zones[:8])}"
            ]
            return projection

        # An elliptical front: the fire runs with the wind and creeps against it. Radius along the wind
        # bearing grows at the wind-boosted rate, across it at the base rate.
        boost = 1.0 + (WIND_FACTOR - 1.0) * min(1.0, wind_mps / 10.0) if wind_mps > 0 else 1.0
        downwind_m = FIRE_SPREAD_MPS * boost * horizon_s
        crosswind_m = FIRE_SPREAD_MPS * horizon_s

        projection.assumptions = [
            f"fire spreads at {FIRE_SPREAD_MPS} m/s across open yard, which is an estimate and not a "
            f"measurement",
            f"wind at {wind_mps:.1f} m/s bearing {bearing:.0f}° multiplies downwind spread by {boost:.1f}",
            "the site is treated as uniformly combustible; surfaces, firebreaks and suppression are not "
            "modelled, so this is a worst case",
        ]

        # Who is inside the ellipse, and when the front reaches them.
        reached: list[tuple[SimEntity, float]] = []
        for entity in world.entities:
            distance = entity.distance_to(origin.lat, origin.lon)
            aligned = _alignment(origin.lat, origin.lon, entity.lat, entity.lon, bearing)
            # Effective spread rate in this entity's direction, interpolated between crosswind and downwind.
            rate = FIRE_SPREAD_MPS * (1.0 + (boost - 1.0) * max(0.0, aligned))
            arrival_s = distance / max(rate, 1e-6)
            if arrival_s <= horizon_s:
                reached.append((entity, arrival_s))
        reached.sort(key=lambda pair: pair[1])

        zones_reached = [
            zone
            for zone in world.zones
            if zone.zone_id != origin_id
            and zone.lat
            and _reaches(origin, zone, bearing, boost, horizon_s)
        ]

        projection.impacted_entities = [entity.entity_id for entity, _ in reached]
        people = [entity for entity, _ in reached if entity.type == "person"]
        projection.summary = (
            f"A fire in {origin.name} would reach about {downwind_m:.0f} m downwind and {crosswind_m:.0f} m "
            f"across wind within {horizon_s / 60:.0f} min, affecting {len(reached)} entities "
            f"({len(people)} of them people) and reaching {len(zones_reached)} other zone(s)."
        )
        projection.kpi_deltas = {
            "entities_at_risk": float(len(reached)),
            "people_at_risk": float(len(people)),
            "zones_at_risk": float(len(zones_reached)),
            "downwind_reach_m": round(downwind_m, 1),
        }
        projection.detail = {
            "origin": origin.zone_id,
            "downwind_m": round(downwind_m, 1),
            "crosswind_m": round(crosswind_m, 1),
            "zones_reached": [zone.zone_id for zone in zones_reached],
            "first_affected": [
                {
                    "entity_id": entity.entity_id,
                    "label": entity.label,
                    "type": entity.type,
                    "arrives_in_s": round(arrival_s, 0),
                }
                for entity, arrival_s in reached[:10]
            ],
        }
        # People first, always. A projection that lists an evacuation and a gate closure in arbitrary order
        # has buried the only recommendation that matters.
        if people:
            projection.recommendations.append(
                f"Evacuate {len(people)} person(s); the nearest is reached in "
                f"{min(arrival_s for entity, arrival_s in reached if entity.type == 'person'):.0f}s"
            )
        if zones_reached:
            projection.recommendations.append(
                f"Clear {', '.join(zone.zone_id for zone in zones_reached[:3])}"
            )
        for entity, arrival_s in reached[:5]:
            projection.add_step(
                arrival_s,
                f"fire front reaches {entity.label or entity.entity_id}",
                entity_id=entity.entity_id,
            )
        return projection


class FloodLevel(Scenario):
    """What is under water at a given level?"""

    name = "flood_level"
    question = "At what water level does each zone flood, and what is affected?"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "level_m": {"type": "number", "description": "Water depth above datum, metres"},
        },
        "required": ["level_m"],
    }

    def project(self, world: WorldSnapshot, params: dict[str, Any]) -> Projection:
        level_m = float(params.get("level_m") or 0.5)
        projection = Projection(summary="", confidence=0.3)

        # No terrain model, and this scenario says so rather than inventing elevations. Zone elevation is read
        # from an attribute when present; otherwise the zone is treated as at datum, which is the pessimistic
        # reading and the only defensible one without survey data.
        elevations = {zone.zone_id: float(zone.capacity or 0) * 0.0 for zone in world.zones}
        projection.assumptions = [
            "there is no terrain model for this site, so every zone is treated as at datum (0 m)",
            "this makes the projection a WORST CASE: with real elevations, higher zones would be spared",
            "drainage, pumps and flow rate are not modelled",
        ]

        flooded = [zone for zone in world.zones if elevations.get(zone.zone_id, 0.0) < level_m]
        affected = [
            entity
            for entity in world.entities
            if any(zone.contains(entity.lat, entity.lon) for zone in flooded)
            or entity.zone_id in {zone.zone_id for zone in flooded}
        ]
        grounded = [entity for entity in affected if not entity.is_airborne]

        projection.impacted_entities = [entity.entity_id for entity in affected]
        projection.summary = (
            f"At {level_m:.2f} m, {len(flooded)} of {len(world.zones)} zones would be affected, "
            f"reaching {len(affected)} entities of which {len(grounded)} cannot leave by air. "
            f"No terrain model exists for this site, so this is a worst case."
        )
        projection.kpi_deltas = {
            "zones_flooded": float(len(flooded)),
            "entities_affected": float(len(affected)),
            "ground_entities_stranded": float(len(grounded)),
            "water_level_m": level_m,
        }
        projection.detail = {"zones": [zone.zone_id for zone in flooded]}
        if grounded:
            projection.recommendations.append(
                f"Move {len(grounded)} ground vehicle(s) and person(s) to higher ground"
            )
        projection.recommendations.append(
            "Survey zone elevations: with a terrain model this projection becomes useful rather than "
            "merely conservative"
        )
        return projection


class DroneBatteryDeath(Scenario):
    """Which drones cannot get home?"""

    name = "drone_battery_death"
    question = "Which drones would not make it back to base on current charge?"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "base_zone_id": {"type": "string", "description": "Where drones return to"},
            "reserve_pct": {"type": "number", "description": "Charge that must remain on landing"},
        },
    }

    def project(self, world: WorldSnapshot, params: dict[str, Any]) -> Projection:
        base_id = str(params.get("base_zone_id") or "")
        reserve_pct = float(params.get("reserve_pct") or 20.0)
        base = world.zone(base_id) if base_id else next(iter(world.zones), None)

        projection = Projection(summary="", confidence=0.5)
        drones = world.of_type("drone")
        # Every return path is quantified, including the trivial ones. "0 of 0 drones at risk" tells a
        # reader the question was asked and answered; an empty dict looks identical to a crash.
        if not drones:
            projection.summary = "There are no drones on site, so none can run out of battery."
            projection.confidence = 1.0
            projection.kpi_deltas = {"drones_at_risk": 0.0, "drones_total": 0.0}
            projection.assumptions = ["there are no drones in the snapshot"]
            return projection
        if base is None:
            projection.summary = "No base zone is defined, so return distance cannot be computed."
            projection.confidence = 0.0
            projection.kpi_deltas = {"drones_at_risk": 0.0, "drones_total": float(len(drones))}
            projection.assumptions = [
                "a base zone is required to compute a return leg; none was given"
            ]
            return projection

        drain_pct_per_s = DRONE_DRAIN_PCT_PER_MINUTE / 60.0
        projection.assumptions = [
            f"a drone drains {DRONE_DRAIN_PCT_PER_MINUTE:.1f}% per minute of flight, matching the yard "
            f"simulator's own rate",
            f"it flies straight home at {DRONE_SPEED_MPS} m/s",
            f"{reserve_pct:.0f}% must remain on landing",
            "wind, payload and hover time are not modelled, so this is optimistic",
        ]

        at_risk: list[dict[str, Any]] = []
        for drone in drones:
            if drone.battery_pct is None:
                continue
            flight_s = drone.distance_to(base.lat, base.lon) / DRONE_SPEED_MPS
            needed = flight_s * drain_pct_per_s + reserve_pct
            margin = drone.battery_pct - needed
            if margin < 0:
                at_risk.append(
                    {
                        "entity_id": drone.entity_id,
                        "label": drone.label,
                        "battery_pct": round(drone.battery_pct, 1),
                        "needed_pct": round(needed, 1),
                        "short_by_pct": round(-margin, 1),
                        "distance_m": round(drone.distance_to(base.lat, base.lon), 0),
                    }
                )

        projection.impacted_entities = [item["entity_id"] for item in at_risk]
        if at_risk:
            worst = max(at_risk, key=lambda item: item["short_by_pct"])
            projection.summary = (
                f"{len(at_risk)} of {len(drones)} drone(s) cannot reach {base.name} with "
                f"{reserve_pct:.0f}% in reserve. The worst is {worst['label'] or worst['entity_id']}, "
                f"short by {worst['short_by_pct']}% over {worst['distance_m']:.0f} m."
            )
            projection.recommendations = [
                f"Recall {len(at_risk)} drone(s) now",
                "Or land them at the nearest zone rather than attempting the return leg",
            ]
        else:
            projection.summary = f"All {len(drones)} drone(s) can reach {base.name} with {reserve_pct:.0f}% in reserve."
            projection.confidence = 0.65
        projection.kpi_deltas = {
            "drones_at_risk": float(len(at_risk)),
            "drones_total": float(len(drones)),
        }
        projection.detail = {"at_risk": at_risk}
        return projection


class BridgeCollapse(Scenario):
    """What is cut off if a route is severed?"""

    name = "bridge_collapse"
    question = "If a route is severed, which zones are cut off and what is stranded?"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "zone_id": {
                "type": "string",
                "description": "The zone the severed route passes through",
            },
            "radius_m": {"type": "number", "description": "How wide the severed corridor is"},
        },
        "required": ["zone_id"],
    }

    def project(self, world: WorldSnapshot, params: dict[str, Any]) -> Projection:
        cut_id = str(params.get("zone_id") or "")
        radius_m = float(params.get("radius_m") or 60.0)
        cut = world.zone(cut_id)

        projection = Projection(summary="", confidence=0.35)
        if cut is None:
            projection.summary = (
                f"There is no zone called {cut_id!r}, so a route through it cannot be cut."
            )
            projection.confidence = 0.0
            projection.kpi_deltas = {"zones_isolated": 0.0, "entities_stranded": 0.0}
            projection.assumptions = [
                f"{cut_id!r} does not exist; the known zones are "
                f"{', '.join(zone.zone_id for zone in world.zones[:8])}"
            ]
            return projection

        # No road graph, and that is the honest limitation. Reachability is approximated geometrically: a
        # zone is cut off if the straight line from it to the nearest gate passes within `radius_m` of the
        # severed point. That is a crude proxy and the explanation says so — with a real road network this
        # becomes a graph cut, which is the correct algorithm.
        gates = [zone for zone in world.zones if zone.is_gate]
        projection.assumptions = [
            "this site has no road graph, so reachability is approximated by straight-line paths to the "
            "nearest gate",
            f"a path is considered severed if it passes within {radius_m:.0f} m of {cut_id}",
            "with a real road network this becomes a graph cut, which would be exact rather than indicative",
        ]

        isolated: list[str] = []
        for zone in world.zones:
            if zone.zone_id == cut_id or zone.is_gate:
                continue
            if not gates:
                continue
            blocked = all(
                _segment_distance(zone.lat, zone.lon, gate.lat, gate.lon, cut.lat, cut.lon)
                < radius_m
                for gate in gates
            )
            if blocked:
                isolated.append(zone.zone_id)

        stranded = [
            entity
            for entity in world.entities
            if entity.zone_id in set(isolated) and not entity.is_airborne
        ]
        projection.impacted_entities = [entity.entity_id for entity in stranded]
        projection.summary = (
            f"Severing the route through {cut.name} would isolate {len(isolated)} zone(s) from every gate, "
            f"stranding {len(stranded)} ground entities. Airborne units are unaffected."
        )
        projection.kpi_deltas = {
            "zones_isolated": float(len(isolated)),
            "entities_stranded": float(len(stranded)),
            "gates_reachable": float(len(gates)),
        }
        projection.detail = {"isolated_zones": isolated}
        if stranded:
            projection.recommendations.append(
                f"Move {len(stranded)} ground entities out of {', '.join(isolated[:3])} before any "
                "planned closure"
            )
        projection.recommendations.append(
            "Model the site's road network to make this projection exact"
        )
        return projection


# ---------------------------------------------------------------------------------- helpers
def _throughput_per_hour(servers: int) -> float:
    """Movements an hour, given N docks each taking DOCK_SERVICE_S."""
    return 3600.0 / DOCK_SERVICE_S * max(servers, 0)


def _alignment(
    origin_lat: float, origin_lon: float, lat: float, lon: float, bearing_deg: float
) -> float:
    """How aligned a direction is with the wind: 1 downwind, 0 crosswind, -1 upwind."""
    mean_lat = math.radians((origin_lat + lat) / 2)
    dy = lat - origin_lat
    dx = (lon - origin_lon) * math.cos(mean_lat)
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return 1.0
    # Bearing is clockwise from north, so north is +y and east is +x.
    wind = math.radians(bearing_deg)
    wind_x, wind_y = math.sin(wind), math.cos(wind)
    norm = math.hypot(dx, dy)
    return (dx * wind_x + dy * wind_y) / norm


def _reaches(origin: Any, zone: Any, bearing_deg: float, boost: float, horizon_s: float) -> bool:
    distance = math.hypot(
        (zone.lat - origin.lat) * 111_320.0,
        (zone.lon - origin.lon) * 111_320.0 * math.cos(math.radians(origin.lat)),
    )
    aligned = _alignment(origin.lat, origin.lon, zone.lat, zone.lon, bearing_deg)
    rate = FIRE_SPREAD_MPS * (1.0 + (boost - 1.0) * max(0.0, aligned))
    return distance <= rate * horizon_s


def _segment_distance(ax: float, ay: float, bx: float, by: float, px: float, py: float) -> float:
    """Metres from point P to segment AB, in degrees converted to metres."""
    scale_lat = 111_320.0
    scale_lon = 111_320.0 * math.cos(math.radians(ax))
    ax_m, ay_m = ax * scale_lat, ay * scale_lon
    bx_m, by_m = bx * scale_lat, by * scale_lon
    px_m, py_m = px * scale_lat, py * scale_lon
    dx, dy = bx_m - ax_m, by_m - ay_m
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-9:
        return math.hypot(px_m - ax_m, py_m - ay_m)
    t = max(0.0, min(1.0, ((px_m - ax_m) * dx + (py_m - ay_m) * dy) / length_sq))
    return math.hypot(px_m - (ax_m + t * dx), py_m - (ay_m + t * dy))


SCENARIOS: dict[str, Scenario] = {
    scenario.name: scenario
    for scenario in (
        GateClosure(),
        DockBreakdown(),
        FireSpread(),
        FloodLevel(),
        DroneBatteryDeath(),
        BridgeCollapse(),
    )
}


__all__ = [
    "DOCK_SERVICE_S",
    "DRONE_DRAIN_PCT_PER_MINUTE",
    "DRONE_SPEED_MPS",
    "FIRE_SPREAD_MPS",
    "GROUND_SPEED_MPS",
    "SCENARIOS",
    "WIND_FACTOR",
    "BridgeCollapse",
    "DockBreakdown",
    "DroneBatteryDeath",
    "FireSpread",
    "FloodLevel",
    "GateClosure",
    "Projection",
    "Scenario",
]
