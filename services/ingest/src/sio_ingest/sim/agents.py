"""Agent behaviour for the yard simulator.

These are deliberately *scripted state machines*, not random walks. The demo has to produce the
PRD's use cases on purpose: a truck that dwells more than fifteen minutes (UC1), a worker who
enters a restricted zone, yard congestion that can be forecast (UC4). Random motion would produce
none of those reliably, and a demo that depends on luck is not a demo.

Everything is driven by a seeded RNG, so `SIO_SIM_SEED` reproduces the same yard exactly — which
is what makes the e2e scenario tests deterministic.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sio_schemas import EntityType, Geo, Velocity

from ..site import Point, Site, to_geo


class TruckState(StrEnum):
    APPROACHING = "approaching"
    AT_GATE = "at_gate"
    DRIVING_TO_DOCK = "driving_to_dock"
    DOCKED = "docked"
    DRIVING_TO_EXIT = "driving_to_exit"
    DEPARTED = "departed"


@dataclass
class Kinematics:
    """Where an agent is and how fast it is going, in local metres."""

    east: float
    north: float
    speed_mps: float = 0.0
    heading_deg: float = 0.0

    @property
    def geo(self) -> Geo:
        return to_geo(self.east, self.north)

    @property
    def velocity(self) -> Velocity:
        radians = math.radians(self.heading_deg)
        return Velocity(
            north=self.speed_mps * math.cos(radians), east=self.speed_mps * math.sin(radians)
        )


@dataclass
class Agent:
    """Base agent: follows a waypoint path with a speed limit and positional noise."""

    agent_id: str
    entity_type: EntityType
    label: str
    kinematics: Kinematics
    site: Site
    rng: random.Random
    max_speed_mps: float = 5.0
    path: list[Point] = field(default_factory=list)
    path_index: int = 0
    wait_until: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    has_gps: bool = True
    active: bool = True
    spawned_at: float = 0.0
    """Simulation clock reading when this agent appeared.

    Carried so the entity it produces can report a real `first_seen`. Without it the simulator built
    a fresh Entity every tick and pydantic's default_factory stamped first_seen = last_seen = now, so
    every dwell time in the UI read as zero — the graph store's merge fixed the stored value but the
    live stream still carried the wrong one.
    """
    _path_completed: bool = False

    # ------------------------------------------------------------------ movement
    def set_path(self, names: list[str]) -> None:
        self.path = [self.site.waypoint(name) for name in names]
        self.path_index = 0
        self._path_completed = False

    @property
    def target(self) -> Point | None:
        if self.path_index >= len(self.path):
            return None
        return self.path[self.path_index]

    def advance(self, dt: float, now: float) -> None:
        """Move toward the current waypoint. Returns silently when waiting or finished."""
        if now < self.wait_until:
            self.kinematics.speed_mps = 0.0
            return
        target = self.target
        if target is None:
            self.kinematics.speed_mps = 0.0
            # The path is finished *and* any wait has elapsed, so let the agent decide what next.
            # Without this hook an agent whose state machine transitions after a wait — a truck
            # pausing at the gate for its RFID check — would sit there forever: `on_waypoint_reached`
            # only fires on arrival, and there are no waypoints left to arrive at. Six trucks
            # accumulated at Gate A and never docked, which also meant no dwell data for UC1.
            if not self._path_completed:
                self._path_completed = True
                self.on_path_complete(now)
            return

        dx = target.east - self.kinematics.east
        dy = target.north - self.kinematics.north
        distance = math.hypot(dx, dy)
        if distance < 1.5:
            self.path_index += 1
            self.on_waypoint_reached(target, now)
            return

        # Ease off near the waypoint so vehicles do not pivot at full speed — it looks wrong on
        # the map and produces implausible velocity readings for the fusion filter.
        speed = min(self.max_speed_mps, max(1.0, distance / 3.0))
        step = min(distance, speed * dt)
        self.kinematics.east += dx / distance * step
        self.kinematics.north += dy / distance * step
        self.kinematics.speed_mps = step / dt if dt > 0 else 0.0
        self.kinematics.heading_deg = (math.degrees(math.atan2(dx, dy)) + 360) % 360

    def on_waypoint_reached(self, waypoint: Point, now: float) -> None:
        """Called on arrival at each waypoint."""

    def on_path_complete(self, now: float) -> None:
        """Called once when the path is exhausted and any wait has elapsed.

        This is where a state machine that pauses before moving on belongs — the transition cannot
        hang off waypoint arrival, because after a wait there is no further waypoint to arrive at.
        """

    def wait(self, now: float, seconds: float) -> None:
        """Pause here for ``seconds``, then fire :meth:`on_path_complete` again.

        Use this rather than assigning ``wait_until`` directly. The hook is latched so it fires once
        per completed path, and a state that pauses *without* setting a new path (a truck dwelling at
        a dock) would otherwise never be called again — trucks docked and then stayed docked forever.
        Resetting the latch here is what makes "wait, then decide" work as a state.
        """
        self.wait_until = now + seconds
        self._path_completed = False

    def gps_reading(self) -> Geo:
        """Position with GPS-like noise.

        ~2 m of scatter, because fusion has to earn its keep: if the simulator emitted perfect
        positions, the EKF and the association gating would never be exercised.
        """
        return to_geo(
            self.kinematics.east + self.rng.gauss(0, 1.4),
            self.kinematics.north + self.rng.gauss(0, 1.4),
        )

    @property
    def zone_id(self) -> str | None:
        zone = self.site.zone_at(self.kinematics.east, self.kinematics.north)
        return zone.zone_id if zone else None

    def state_summary(self) -> dict[str, Any]:
        return {"state": self.attributes.get("state", "active")}


QUEUE_SPACING_M = 16.0
"""Gap between queued trucks. Six trucks waiting at the same coordinate rendered as one dot with
six overprinted labels; a real queue is a line."""


@dataclass
class Truck(Agent):
    """Arrive, queue at the gate, dock, dwell, depart.

    Dwell time is drawn so that a *predictable minority* exceed fifteen minutes: UC1 ("every truck
    that entered today and stayed more than 15 minutes") needs both positives and negatives in the
    data, or the query proves nothing.
    """

    state: TruckState = TruckState.APPROACHING
    dock_id: str | None = None
    dwell_s: float = 600.0
    plate: str = ""
    arrived_at: float = 0.0
    docked_at: float = 0.0
    queue_slot: int = 0

    def start(self, now: float) -> None:
        self.state = TruckState.APPROACHING
        self.arrived_at = now
        self.spawned_at = now
        self.attributes.update({"state": str(self.state), "plate": self.plate})
        self.set_path(["gate_a_approach", "gate_a"])

    def gps_reading(self) -> Geo:
        """Position, offset by the truck's queue slot while it waits at the gate.

        Only applied while stationary at the gate: on the move, the queue slot is meaningless and the
        offset would look like a lane change.
        """
        if self.state is TruckState.AT_GATE and self.queue_slot:
            return to_geo(
                self.kinematics.east - self.queue_slot * QUEUE_SPACING_M + self.rng.gauss(0, 1.0),
                self.kinematics.north + self.rng.gauss(0, 1.0),
            )
        return super().gps_reading()

    def on_waypoint_reached(self, waypoint: Point, now: float) -> None:
        if waypoint.name == "gate_a" and self.state is TruckState.APPROACHING:
            self.state = TruckState.AT_GATE
            # Gate check: an RFID read plus a guard glance. This pause is why the machine must be
            # able to advance on *path completion* and not only on waypoint arrival — see below.
            self.wait(now, self.rng.uniform(8, 25))
            self.attributes["state"] = str(self.state)

    def on_path_complete(self, now: float) -> None:
        """Advance to the next leg of the journey: gate → dock → dwell → exit → gone.

        Every transition hangs off *finishing a leg*, not off arriving at a particular named
        waypoint. The earlier version keyed on arrival, which meant the post-gate transition could
        never fire: after the RFID wait the path was already exhausted, so no further arrival ever
        happened and all six trucks sat at Gate A forever — no docking, no dwell data for UC1, and
        six labels overprinted on one pixel.
        """
        index = (self.dock_id or "dock_1").rsplit("_", 1)[-1]

        if self.state is TruckState.AT_GATE:
            self.state = TruckState.DRIVING_TO_DOCK
            self.attributes["state"] = str(self.state)
            self.set_path(
                [
                    "gate_a_inner",
                    "south_west",
                    "west_link",
                    "north_west",
                    f"dock_{index}_approach",
                    f"dock_{index}_bay",
                ]
            )
            return

        if self.state is TruckState.DRIVING_TO_DOCK:
            self.state = TruckState.DOCKED
            self.docked_at = now
            self.wait(now, self.dwell_s)
            self.attributes.update({"state": str(self.state), "dock": self.dock_id})
            return

        if self.state is TruckState.DOCKED:
            self.state = TruckState.DRIVING_TO_EXIT
            self.attributes["state"] = str(self.state)
            self.set_path(
                [
                    f"dock_{index}_approach",
                    "north_east",
                    "east_link",
                    "south_east",
                    "gate_b_inner",
                    "gate_b",
                    "gate_b_exit",
                ]
            )
            return

        if self.state is TruckState.DRIVING_TO_EXIT:
            self.state = TruckState.DEPARTED
            self.active = False
            self.attributes["state"] = str(self.state)

    def state_summary(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "plate": self.plate,
            "dock": self.dock_id,
            "dwell_target_s": round(self.dwell_s),
        }


@dataclass
class Forklift(Agent):
    """Shuttles between dock bays. Never leaves the apron, no plate, no GPS of its own."""

    loop: list[str] = field(default_factory=list)

    def start(self, now: float) -> None:
        docks = self.site.dock_ids()
        first, second = self.rng.sample(docks, 2)
        # `_side`, not `_bay`: the bay waypoint is where the truck parks, and sending the forklift
        # to the same coordinate left it exactly 1 cm from the truck — its dot hidden underneath and
        # its label overprinting the truck's plate into an unreadable smear.
        self.loop = [
            f"{first}_side",
            f"{first}_approach",
            f"{second}_approach",
            f"{second}_side",
        ]
        self.spawned_at = now
        self.set_path(self.loop)
        self.attributes["state"] = "shuttling"

    def on_waypoint_reached(self, waypoint: Point, now: float) -> None:
        if self.path_index >= len(self.path):
            # Loading and unloading pauses, then reverse the loop.
            self.wait(now, self.rng.uniform(10, 40))
            self.loop.reverse()
            self.set_path(self.loop)


@dataclass
class Worker(Agent):
    """Walks between the office, the docks and occasionally somewhere they should not be.

    The restricted-zone excursion is intentional and rate-limited: it is the seed for the
    `unauthorized_entry` rule in Phase 3, and it needs to happen often enough to demo but rarely
    enough to look like an exception.
    """

    trespass_probability: float = 0.12

    def start(self, now: float) -> None:
        self.spawned_at = now
        self.attributes["state"] = "walking"
        self._choose_destination(now)

    def _choose_destination(self, now: float) -> None:
        if self.rng.random() < self.trespass_probability:
            self.set_path(["south_mid", "fuel_store"])
            self.attributes["state"] = "entering_restricted"
            return
        docks = self.site.dock_ids()
        dock = self.rng.choice(docks)
        self.set_path(
            self.rng.choice(
                [
                    ["office_door", "north_west", f"{dock}_approach"],
                    ["north_mid", "yard_centre", "south_mid"],
                    ["north_east", "north_mid", "north_west"],
                ]
            )
        )
        self.attributes["state"] = "walking"

    def on_waypoint_reached(self, waypoint: Point, now: float) -> None:
        if self.path_index >= len(self.path):
            self.wait(now, self.rng.uniform(15, 90))
            self._choose_destination(now)


@dataclass
class Drone(Agent):
    """Patrols the perimeter and drains a battery.

    The battery matters for the demo: it is the target of one forecast (PRD M10 "drone battery")
    and the trigger for one simulation scenario (M11 "drone battery death").
    """

    battery_pct: float = 100.0
    drain_per_minute: float = 1.6
    altitude_m: float = 35.0

    def start(self, now: float) -> None:
        self.spawned_at = now
        self.set_path(self.site.routes["patrol"])
        self.attributes.update({"state": "patrolling", "battery_pct": self.battery_pct})

    def advance(self, dt: float, now: float) -> None:
        super().advance(dt, now)
        self.battery_pct = max(0.0, self.battery_pct - self.drain_per_minute * dt / 60.0)
        self.attributes["battery_pct"] = round(self.battery_pct, 1)
        if self.battery_pct < 20 and self.attributes.get("state") != "returning":
            self.attributes["state"] = "returning"
            self.set_path(["yard_centre", "office_door"])

    def on_waypoint_reached(self, waypoint: Point, now: float) -> None:
        if self.path_index >= len(self.path):
            if self.battery_pct < 20:
                # Swap the battery rather than vanishing: an entity disappearing from the map for
                # no visible reason is a worse demo than a two-minute pause.
                self.wait(now, 120)
                self.battery_pct = 100.0
                self.attributes["state"] = "patrolling"
            self.set_path(self.site.routes["patrol"])

    def gps_reading(self) -> Geo:
        geo = super().gps_reading()
        return Geo(lat=geo.lat, lon=geo.lon, alt=self.altitude_m + self.rng.gauss(0, 0.6))

    def state_summary(self) -> dict[str, Any]:
        return {
            "state": self.attributes.get("state", "patrolling"),
            "battery_pct": round(self.battery_pct, 1),
        }


PLATE_LETTERS = "BCDFGHJKLMNPRSTVWXYZ"


def make_plate(rng: random.Random) -> str:
    letters = "".join(rng.choice(PLATE_LETTERS) for _ in range(3))
    return f"{letters}-{rng.randint(100, 999)}"


class Population:
    """Creates and maintains the cast of agents.

    Trucks are replaced as they depart, so the yard keeps a steady occupancy instead of emptying
    out five minutes into a demo.
    """

    def __init__(
        self,
        site: Site,
        rng: random.Random,
        *,
        trucks: int,
        forklifts: int,
        people: int,
        drones: int,
    ) -> None:
        self.site = site
        self.rng = rng
        self.target_trucks = trucks
        self.agents: list[Agent] = []
        self._counter = 0
        self._docks_in_use: set[str] = set()

        for _ in range(trucks):
            self.spawn_truck(0.0, stagger=True)
        for _ in range(forklifts):
            self.spawn_forklift(0.0)
        for _ in range(people):
            self.spawn_worker(0.0)
        for _ in range(drones):
            self.spawn_drone(0.0)

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:04d}"

    # -------------------------------------------------------------------- spawns
    def spawn_truck(self, now: float, *, stagger: bool = False) -> Truck:
        free_docks = [dock for dock in self.site.dock_ids() if dock not in self._docks_in_use]
        dock = self.rng.choice(free_docks or self.site.dock_ids())
        self._docks_in_use.add(dock)
        plate = make_plate(self.rng)
        start = self.site.waypoint("gate_a_approach")
        truck = Truck(
            agent_id=self._next_id("truck"),
            entity_type=EntityType.TRUCK,
            label=f"Truck {plate}",
            kinematics=Kinematics(east=start.east - self.rng.uniform(0, 40), north=start.north),
            site=self.site,
            rng=self.rng,
            max_speed_mps=5.5,
            dock_id=dock,
            plate=plate,
            # One truck in three dwells past the fifteen-minute threshold, so UC1 has both
            # positives and negatives to distinguish.
            dwell_s=self.rng.choice([300, 480, 600, 960, 1_200, 1_500]),
        )
        truck.queue_slot = sum(
            1
            for agent in self.agents
            if isinstance(agent, Truck) and agent.state is TruckState.AT_GATE
        )
        truck.start(now)
        if stagger:
            truck.wait(now, self.rng.uniform(0, 60))
        self.agents.append(truck)
        return truck

    def spawn_forklift(self, now: float) -> Forklift:
        start = self.site.waypoint("dock_3_approach")
        forklift = Forklift(
            agent_id=self._next_id("forklift"),
            entity_type=EntityType.FORKLIFT,
            label=f"Forklift {self._counter}",
            kinematics=Kinematics(east=start.east + self.rng.uniform(-20, 20), north=start.north),
            site=self.site,
            rng=self.rng,
            max_speed_mps=3.0,
            has_gps=False,
        )
        forklift.start(now)
        self.agents.append(forklift)
        return forklift

    def spawn_worker(self, now: float) -> Worker:
        start = self.site.waypoint("office_door")
        worker = Worker(
            agent_id=self._next_id("worker"),
            entity_type=EntityType.PERSON,
            label=f"Worker {self._counter}",
            kinematics=Kinematics(east=start.east + self.rng.uniform(-8, 8), north=start.north),
            site=self.site,
            rng=self.rng,
            max_speed_mps=1.4,
            has_gps=False,
        )
        worker.start(now)
        self.agents.append(worker)
        return worker

    def spawn_drone(self, now: float) -> Drone:
        start = self.site.waypoint("yard_centre")
        drone = Drone(
            agent_id=self._next_id("drone"),
            entity_type=EntityType.DRONE,
            label=f"Drone {self._counter}",
            kinematics=Kinematics(east=start.east, north=start.north),
            site=self.site,
            rng=self.rng,
            max_speed_mps=9.0,
        )
        drone.start(now)
        self.agents.append(drone)
        return drone

    # ---------------------------------------------------------------------- step
    def step(self, dt: float, now: float) -> None:
        for agent in self.agents:
            if agent.active:
                agent.advance(dt, now)

        departed = [a for a in self.agents if isinstance(a, Truck) and not a.active]
        for truck in departed:
            self._docks_in_use.discard(truck.dock_id or "")
            self.agents.remove(truck)
        for _ in range(len(departed)):
            self.spawn_truck(now)

        current_trucks = sum(1 for a in self.agents if isinstance(a, Truck))
        while current_trucks < self.target_trucks:
            self.spawn_truck(now)
            current_trucks += 1

    def active_agents(self) -> Iterator[Agent]:
        return (agent for agent in self.agents if agent.active)

    def by_id(self, agent_id: str) -> Agent | None:
        return next((agent for agent in self.agents if agent.agent_id == agent_id), None)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for agent in self.agents:
            key = str(agent.entity_type)
            counts[key] = counts.get(key, 0) + 1
        return counts
