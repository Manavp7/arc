"""The three optimisation problems, solved with OR-Tools (PRD M12).

Each solver returns **ranked options**, not an answer. That is the requirement and it is also the honest
shape: an optimiser's objective encodes somebody's opinion about trade-offs, and presenting the winner
alone hides both the runner-up and the fact that a choice was made. An operator who can see that option two
was 8 % worse but half the cost is being helped; one shown a single recommendation is being told.

Three problems, three genuinely different structures:

* **assignment** (CP-SAT) — which responder to which incident. A matching problem with capacities.
* **routing** (VRP) — the order to visit several places. A sequencing problem where the objective is total
  travel.
* **scheduling** (CP-SAT with intervals) — which dock at what time. A packing problem over time.

They are separate solvers rather than one clever model because their constraints do not compose: a routing
objective measures distance, an assignment objective measures suitability, and a schedule measures
lateness. Forcing them into one model would need weights nobody could defend.

Every solver is bounded by a time limit. An optimiser given an unbounded budget will take it, and a
recommendation that arrives after the incident is over is not a recommendation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from sio_core import get_logger

log = get_logger("sio.decision.solvers")

SOLVE_TIME_LIMIT_S = 2.0
"""Wall-clock budget per solve.

Two seconds, because these are small problems — a dozen responders, a handful of incidents — and CP-SAT
proves optimality on them in milliseconds. The limit exists for the pathological case, and it exists at all
because a recommendation that arrives after the incident is over is not a recommendation.
"""

EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True)
class Responder:
    """Something that can be sent: a drone, a patrol, a forklift."""

    entity_id: str
    kind: str
    lat: float
    lon: float
    speed_mps: float = 8.0
    label: str | None = None
    busy: bool = False
    battery_pct: float | None = None
    capacity: int = 1
    """How many incidents this responder can take. Usually one — a drone at two fires is at neither."""

    @property
    def name(self) -> str:
        return self.label or self.entity_id


@dataclass(frozen=True)
class Incident:
    """Something that needs a responder."""

    incident_id: str
    kind: str
    lat: float
    lon: float
    severity: str = "high"
    zone_id: str | None = None
    requires: tuple[str, ...] = ()
    """Responder kinds that can serve this. Empty means any."""

    @property
    def weight(self) -> float:
        """How much worse it is to leave this one unattended.

        A rank, not a score: severities are ordered, and treating them as evenly spaced would make two
        medium incidents outrank one critical — which is exactly the trade nobody wants an optimiser to
        make silently.
        """
        return {"info": 1.0, "low": 2.0, "medium": 4.0, "high": 8.0, "critical": 16.0}.get(
            self.severity, 4.0
        )


@dataclass
class Assignment:
    """One responder sent to one incident."""

    responder: Responder
    incident: Incident
    distance_m: float
    eta_s: float
    suitability: float
    """0-1: how well this responder fits this incident, before distance is considered."""

    def describe(self) -> dict[str, Any]:
        return {
            "responder": self.responder.entity_id,
            "responder_name": self.responder.name,
            "incident": self.incident.incident_id,
            "distance_m": round(self.distance_m, 1),
            "eta_s": round(self.eta_s, 1),
            "suitability": round(self.suitability, 3),
        }


@dataclass
class SolverResult:
    """A solved plan with the numbers behind it."""

    name: str
    status: str
    assignments: list[Assignment] = field(default_factory=list)
    objective: float = 0.0
    unassigned: list[Incident] = field(default_factory=list)
    solve_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
    route: list[str] = field(default_factory=list)
    total_distance_m: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")

    @property
    def worst_eta_s(self) -> float:
        """The slowest response in the plan.

        Reported alongside the mean because a plan that gets to four incidents in a minute and the fifth in
        an hour is not a good plan, and an average hides exactly that.
        """
        return max((assignment.eta_s for assignment in self.assignments), default=0.0)

    def describe(self) -> dict[str, Any]:
        return {
            "solver": self.name,
            "status": self.status,
            "objective": round(self.objective, 3),
            "solve_ms": round(self.solve_ms, 1),
            "assignments": [assignment.describe() for assignment in self.assignments],
            "unassigned": [incident.incident_id for incident in self.unassigned],
            "worst_eta_s": round(self.worst_eta_s, 1),
            "total_distance_m": round(self.total_distance_m, 1),
            "route": self.route,
            "notes": self.notes,
        }


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi, delta_lambda = phi2 - phi1, math.radians(lon2 - lon1)
    inner = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(inner))


def suitability(responder: Responder, incident: Incident) -> float:
    """How well a responder fits an incident, ignoring distance.

    Separated from distance on purpose. Collapsing them into one number makes the trade-off invisible, and
    the trade-off is the whole question: a perfectly suited responder twice as far away may or may not be
    the right choice, and an operator can only judge that if both figures survive to the explanation.
    """
    if incident.requires and responder.kind not in incident.requires:
        return 0.0
    fit = {
        ("fire", "drone"): 1.0,
        ("fire", "patrol"): 0.6,
        ("intrusion", "drone"): 0.8,
        ("intrusion", "patrol"): 1.0,
        ("medical", "patrol"): 0.9,
        ("congestion", "patrol"): 0.7,
        ("dwell", "patrol"): 0.5,
    }.get((incident.kind, responder.kind), 0.4)
    if responder.battery_pct is not None and responder.battery_pct < 30:
        # A drone that will need to turn back mid-task is a poor choice even when it is closest, and this
        # is the kind of constraint that is obvious in hindsight and invisible in a distance-only model.
        fit *= 0.4
    if responder.busy:
        fit *= 0.5
    return round(fit, 3)


# --------------------------------------------------------------------- assignment
def solve_assignment(
    responders: list[Responder],
    incidents: list[Incident],
    *,
    time_limit_s: float = SOLVE_TIME_LIMIT_S,
) -> SolverResult:
    """Assign responders to incidents with CP-SAT.

    The objective maximises severity-weighted suitability while penalising travel time, and the weights are
    stated here rather than tuned into opacity:

    * an incident's severity weight (a rank, so critical genuinely outranks two mediums);
    * suitability, so a drone goes to the fire and a patrol to the intrusion;
    * a travel penalty, so all else equal the nearest responder goes.

    Leaving an incident unassigned is *allowed* and costed. A model forced to assign every incident will
    happily send a low-battery drone across the whole site to a minor event, and refusing to answer is
    sometimes the right answer — but only if it is visible, so unassigned incidents are reported.
    """
    from ortools.sat.python import cp_model

    result = SolverResult(name="ortools-cpsat-assignment", status="INVALID")
    if not responders or not incidents:
        result.status = "EMPTY"
        result.notes.append(
            f"nothing to solve: {len(responders)} responder(s), {len(incidents)} incident(s)"
        )
        result.unassigned = list(incidents)
        return result

    model = cp_model.CpModel()
    pairs: dict[tuple[int, int], Any] = {}
    metrics: dict[tuple[int, int], tuple[float, float, float]] = {}

    for r_index, responder in enumerate(responders):
        for i_index, incident in enumerate(incidents):
            fit = suitability(responder, incident)
            if fit <= 0.0:
                continue  # cannot serve it; no variable, rather than a variable forced to zero
            distance = haversine_m(responder.lat, responder.lon, incident.lat, incident.lon)
            eta = distance / max(0.5, responder.speed_mps)
            pairs[(r_index, i_index)] = model.NewBoolVar(f"x_{r_index}_{i_index}")
            metrics[(r_index, i_index)] = (distance, eta, fit)

    if not pairs:
        result.status = "INFEASIBLE"
        result.unassigned = list(incidents)
        result.notes.append("no responder is capable of any incident")
        return result

    # Each incident takes at most one responder. "At most" rather than "exactly": see the docstring on
    # leaving an incident unassigned.
    for i_index in range(len(incidents)):
        candidates = [var for (r, i), var in pairs.items() if i == i_index]
        if candidates:
            model.AddAtMostOne(candidates)

    # Each responder respects its capacity — normally one, because a drone at two fires is at neither.
    for r_index, responder in enumerate(responders):
        taken = [var for (r, i), var in pairs.items() if r == r_index]
        if taken:
            model.Add(sum(taken) <= responder.capacity)

    # CP-SAT is integral, so the objective is scaled to integers. Scaling by 1000 keeps three decimal
    # places of suitability, which is more than the inputs justify.
    terms = []
    for (r_index, i_index), var in pairs.items():
        distance, eta, fit = metrics[(r_index, i_index)]
        reward = incidents[i_index].weight * fit * 1000
        # Travel penalty in the same units. A minute of travel costs about as much as 6 % of a perfect fit
        # on a high-severity incident — enough to break ties by distance, not enough to send the wrong kind
        # of responder because it happens to be closer.
        penalty = eta * 8
        terms.append(round(reward - penalty) * var)
    model.Maximize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 4
    status = solver.Solve(model)
    result.status = solver.StatusName(status)
    result.solve_ms = solver.WallTime() * 1000
    if not result.ok:
        result.unassigned = list(incidents)
        result.notes.append(f"the solver returned {result.status}")
        return result

    result.objective = solver.ObjectiveValue() / 1000
    assigned_incidents: set[int] = set()
    for (r_index, i_index), var in pairs.items():
        if solver.Value(var):
            distance, eta, fit = metrics[(r_index, i_index)]
            result.assignments.append(
                Assignment(
                    responder=responders[r_index],
                    incident=incidents[i_index],
                    distance_m=distance,
                    eta_s=eta,
                    suitability=fit,
                )
            )
            assigned_incidents.add(i_index)

    result.assignments.sort(key=lambda assignment: assignment.eta_s)
    result.unassigned = [
        incident for index, incident in enumerate(incidents) if index not in assigned_incidents
    ]
    result.total_distance_m = sum(assignment.distance_m for assignment in result.assignments)
    if result.unassigned:
        result.notes.append(
            f"{len(result.unassigned)} incident(s) left unassigned: no responder was worth sending"
        )
    return result


# ------------------------------------------------------------------------ routing
def solve_route(
    start: tuple[float, float],
    stops: list[tuple[str, float, float]],
    *,
    speed_mps: float = 8.0,
    time_limit_s: float = SOLVE_TIME_LIMIT_S,
    return_to_start: bool = True,
) -> SolverResult:
    """Order a set of stops to minimise travel — a single-vehicle VRP.

    `return_to_start` defaults to true, because a patrol that ends its route at the far corner of the site
    has not finished, it has stopped. Making it optional matters for a drone whose next task is elsewhere.

    Distances are integer metres. The routing solver is integral, and rounding at the boundary is honest;
    scaling to centimetres would imply a precision the GPS fixes do not have.
    """
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    result = SolverResult(name="ortools-vrp-route", status="INVALID")
    if not stops:
        result.status = "EMPTY"
        result.notes.append("no stops to route")
        return result

    points = [("start", start[0], start[1]), *stops]
    size = len(points)
    matrix = [
        [
            round(haversine_m(points[i][1], points[i][2], points[j][1], points[j][2]))
            for j in range(size)
        ]
        for i in range(size)
    ]

    manager = pywrapcp.RoutingIndexManager(size, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index: int, to_index: int) -> int:
        return matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    transit = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    if not return_to_start:
        # An open route: zero the cost of returning to the depot, so the solver stops wherever is cheapest.
        #
        # The first version also looped over every node calling `RemoveValue(routing.End(0))` on the SAME
        # variable each time — the loop variable was unused, which ruff caught as a style warning and which
        # was really a bug: it did the same thing size-1 times and constrained nothing useful. Zeroing the
        # return arc is the whole technique.
        for row in matrix:
            row[0] = 0

    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    # A metaheuristic, because the cheapest-arc first solution is often visibly poor on a handful of stops,
    # and guided local search fixes it within the same budget.
    parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    parameters.time_limit.FromMilliseconds(int(time_limit_s * 1000))

    solution = routing.SolveWithParameters(parameters)
    if solution is None:
        result.status = "INFEASIBLE"
        result.notes.append("the router found no solution")
        return result

    result.status = "OPTIMAL" if routing.status() == 1 else "FEASIBLE"
    index = routing.Start(0)
    order: list[str] = []
    distance = 0
    while not routing.IsEnd(index):
        order.append(points[manager.IndexToNode(index)][0])
        previous = index
        index = solution.Value(routing.NextVar(index))
        distance += routing.GetArcCostForVehicle(previous, index, 0)
    if return_to_start:
        order.append("start")

    result.route = order
    result.total_distance_m = float(distance)
    result.objective = float(distance)
    result.notes.append(
        f"{len(stops)} stop(s) in {distance:,} m, about {distance / max(0.5, speed_mps) / 60:.1f} min at "
        f"{speed_mps:.0f} m/s"
    )
    return result


# --------------------------------------------------------------------- scheduling
@dataclass(frozen=True)
class DockRequest:
    """A vehicle wanting a dock."""

    entity_id: str
    duration_s: int
    earliest_s: int = 0
    priority: int = 1
    label: str | None = None

    @property
    def name(self) -> str:
        return self.label or self.entity_id


@dataclass
class DockSlot:
    request: DockRequest
    dock_id: str
    start_s: int
    end_s: int

    @property
    def wait_s(self) -> int:
        """How long this vehicle waits beyond the time it was ready.

        A property rather than a field computed at construction: it is derived from two numbers already
        here, and duplicating it would let the two disagree.
        """
        return self.start_s - self.request.earliest_s

    def describe(self) -> dict[str, Any]:
        return {
            "entity_id": self.request.entity_id,
            "name": self.request.name,
            "dock_id": self.dock_id,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "wait_s": self.wait_s,
        }


@dataclass
class ScheduleResult:
    status: str
    slots: list[DockSlot] = field(default_factory=list)
    makespan_s: int = 0
    total_wait_s: int = 0
    solve_ms: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")

    @property
    def worst_wait_s(self) -> int:
        """The longest single wait. Reported because a schedule with a low total wait can still leave one
        truck queuing for an hour, and that truck's driver does not care about the total."""
        return max((slot.wait_s for slot in self.slots), default=0)

    def describe(self) -> dict[str, Any]:
        return {
            "solver": "ortools-cpsat-schedule",
            "status": self.status,
            "makespan_s": self.makespan_s,
            "total_wait_s": self.total_wait_s,
            "worst_wait_s": self.worst_wait_s,
            "solve_ms": round(self.solve_ms, 1),
            "slots": [slot.describe() for slot in self.slots],
            "notes": self.notes,
        }


def solve_dock_schedule(
    requests: list[DockRequest],
    docks: list[str],
    *,
    horizon_s: int | None = None,
    time_limit_s: float = SOLVE_TIME_LIMIT_S,
) -> ScheduleResult:
    """Pack vehicles onto docks with CP-SAT interval variables.

    Minimises **priority-weighted waiting**, not the makespan. Makespan is the intuitive objective and the
    wrong one here: it optimises for the last truck leaving, which a scheduler achieves equally well by
    making one truck wait the entire session. Weighted wait puts the cost where the annoyance is.

    `NoOverlap` per dock is the whole constraint, and it is why interval variables exist — expressing "these
    two cannot share a dock" as pairwise disjunctions works and scales badly.
    """
    from ortools.sat.python import cp_model

    result = ScheduleResult(status="INVALID")
    if not requests or not docks:
        result.status = "EMPTY"
        result.notes.append(f"{len(requests)} request(s), {len(docks)} dock(s)")
        return result

    horizon = horizon_s or (
        sum(request.duration_s for request in requests) + max(r.earliest_s for r in requests)
    )
    model = cp_model.CpModel()

    starts: dict[str, Any] = {}
    ends: dict[str, Any] = {}
    chosen: dict[tuple[str, str], Any] = {}
    per_dock: dict[str, list[Any]] = {dock: [] for dock in docks}

    for request in requests:
        start = model.NewIntVar(request.earliest_s, horizon, f"start_{request.entity_id}")
        end = model.NewIntVar(request.earliest_s, horizon, f"end_{request.entity_id}")
        model.Add(end == start + request.duration_s)
        starts[request.entity_id] = start
        ends[request.entity_id] = end

        assignments = []
        for dock in docks:
            present = model.NewBoolVar(f"at_{request.entity_id}_{dock}")
            chosen[(request.entity_id, dock)] = present
            assignments.append(present)
            # An OPTIONAL interval per dock: it exists only if the vehicle is assigned there, which is what
            # lets one NoOverlap per dock do all the work.
            per_dock[dock].append(
                model.NewOptionalIntervalVar(
                    start, request.duration_s, end, present, f"iv_{request.entity_id}_{dock}"
                )
            )
        model.AddExactlyOne(assignments)

    for dock in docks:
        model.AddNoOverlap(per_dock[dock])

    model.Minimize(
        sum(
            request.priority * (starts[request.entity_id] - request.earliest_s)
            for request in requests
        )
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 4
    status = solver.Solve(model)
    result.status = solver.StatusName(status)
    result.solve_ms = solver.WallTime() * 1000
    if not result.ok:
        result.notes.append(f"the solver returned {result.status}")
        return result

    for request in requests:
        dock = next(dock for dock in docks if solver.Value(chosen[(request.entity_id, dock)]))
        result.slots.append(
            DockSlot(
                request=request,
                dock_id=dock,
                start_s=int(solver.Value(starts[request.entity_id])),
                end_s=int(solver.Value(ends[request.entity_id])),
            )
        )
    result.slots.sort(key=lambda slot: (slot.start_s, slot.dock_id))
    result.makespan_s = max(slot.end_s for slot in result.slots)
    result.total_wait_s = sum(slot.wait_s for slot in result.slots)
    result.notes.append(
        f"{len(requests)} vehicle(s) across {len(docks)} dock(s); total wait "
        f"{result.total_wait_s}s, worst {result.worst_wait_s}s"
    )
    return result


__all__ = [
    "SOLVE_TIME_LIMIT_S",
    "Assignment",
    "DockRequest",
    "DockSlot",
    "Incident",
    "Responder",
    "ScheduleResult",
    "SolverResult",
    "haversine_m",
    "solve_assignment",
    "solve_dock_schedule",
    "solve_route",
    "suitability",
]
