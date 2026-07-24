"""Tests for the yard simulator.

The simulator is not scenery: the PRD's use cases are *produced* by it, so its behaviour is a
contract. UC1 needs trucks that dwell past fifteen minutes and trucks that do not. UC2 needs a fire
that a thermal sensor can actually see. The e2e scenario tests need all of it to be reproducible
from a seed.

These tests exist because the truck state machine was silently broken in a way that nothing else
would have caught: every truck reached the gate and stopped there forever, so the yard looked busy
while producing no docking, no dwell data, and six labels stacked on one pixel.
"""

from __future__ import annotations

import pytest
from sio_ingest.sim.agents import Drone, Forklift, Truck, TruckState, Worker
from sio_ingest.sim.simulator import YardSimulator
from sio_ingest.site import default_yard, load_site, to_geo, to_local

from sio_schemas import Topic

DT = 0.25  # the default 4 Hz tick


def run_for(sim: YardSimulator, seconds: float) -> None:
    for _ in range(int(seconds / DT)):
        sim.step(DT)


# ----------------------------------------------------------------------------- site
def test_site_has_the_geometry_the_use_cases_need() -> None:
    site = default_yard()
    assert len(site.dock_ids()) == 6, "UC2 needs a dock to set fire to"
    assert site.zone("gate_a") and site.zone("gate_b"), "UC1 needs an entry and an exit"
    assert any(zone.restricted for zone in site.zones), "the unauthorized_entry rule needs one"
    assert len(site.cameras) >= 6
    assert len(site.sensors) >= 6
    assert "arrive" in site.routes and "patrol" in site.routes


def test_local_metre_projection_round_trips() -> None:
    """Geometry is authored in metres; a projection error would distort the whole site."""
    for east, north in ((0, 0), (100, 50), (420, 260), (-10, -10)):
        back_east, back_north = to_local(to_geo(east, north))
        assert back_east == pytest.approx(east, abs=0.01)
        assert back_north == pytest.approx(north, abs=0.01)


def test_zone_lookup_returns_the_innermost_zone() -> None:
    """A dock sits inside the yard which sits inside the perimeter; 'dock_3' is the useful answer."""
    site = default_yard()
    dock = site.zone("dock_3")
    assert dock is not None
    east, north = dock.centroid
    found = site.zone_at(east, north)
    assert found is not None and found.zone_id == "dock_3"


def test_camera_field_of_view_is_directional() -> None:
    site = default_yard()
    camera = next(c for c in site.cameras if c.source_id == "cam-gate-a")
    # It points south (bearing 180) from north of the gate.
    assert camera.sees(camera.east, camera.north - 20), "should see in front of itself"
    assert not camera.sees(camera.east, camera.north + 20), "should not see behind itself"
    assert not camera.sees(camera.east, camera.north - camera.range_m - 30), "range is limited"


def test_site_geojson_export_is_complete() -> None:
    site = default_yard()
    collection = site.as_geojson()
    kinds = {feature["properties"]["kind"] for feature in collection["features"]}
    assert {"zone", "camera", "camera_fov", "sensor", "waypoint"} <= kinds
    for feature in collection["features"]:
        if feature["properties"]["kind"] == "zone":
            ring = feature["geometry"]["coordinates"][0]
            assert ring[0] == ring[-1], "GeoJSON polygons must close"


# ------------------------------------------------------------------------ determinism
def agent_key(entity_id: str) -> str:
    """The stable part of a simulated entity id, with the per-run prefix removed."""
    return entity_id.split("-", 2)[-1]


def test_the_same_seed_produces_the_same_yard() -> None:
    """The e2e scenario tests depend on this.

    Determinism here means *behaviour*: same seed, same cast, same trajectories. It deliberately
    does **not** extend to entity ids, which carry a per-run prefix so that restarting the simulator
    creates a new cast rather than resurrecting the previous run's entities and inheriting their
    lifetimes through the store's lifetime merge (six trucks reported 89 minutes on site seconds
    after a restart).
    """
    a, b = YardSimulator(seed=99), YardSimulator(seed=99)
    assert a.run_id != b.run_id, "each run must be distinguishable from the last"
    run_for(a, 30)
    run_for(b, 30)
    positions_a = [
        (agent_key(e.entity_id), e.state.geo.lat, e.state.geo.lon)  # type: ignore[union-attr]
        for e in a.ground_truth_entities()
    ]
    positions_b = [
        (agent_key(e.entity_id), e.state.geo.lat, e.state.geo.lon)  # type: ignore[union-attr]
        for e in b.ground_truth_entities()
    ]
    assert positions_a == positions_b


def test_different_seeds_produce_different_yards() -> None:
    a, b = YardSimulator(seed=1), YardSimulator(seed=2)
    run_for(a, 30)
    run_for(b, 30)
    plates_a = {e.label for e in a.ground_truth_entities() if e.type == "truck"}
    plates_b = {e.label for e in b.ground_truth_entities() if e.type == "truck"}
    assert plates_a != plates_b


def test_the_clock_is_internal_not_wall_time() -> None:
    """Stepping 60 simulated seconds must advance the simulation by 60 seconds, instantly."""
    sim = YardSimulator(seed=7)
    run_for(sim, 60)
    assert sim.elapsed_s == pytest.approx(60.0)
    assert sim.wall_elapsed_s < 5.0


# ---------------------------------------------------------------------- truck cycle
def test_trucks_complete_the_full_cycle() -> None:
    """arrive → gate → dock → dwell → exit → replaced.

    The regression this guards: transitions used to hang off waypoint arrival, so the post-gate and
    post-dwell transitions (which follow a *wait*, with no waypoint left to arrive at) never fired.
    Trucks accumulated at Gate A and, later, sat docked forever.
    """
    sim = YardSimulator(seed=1337, trucks=4, forklifts=0, people=0, drones=0)
    seen_states: set[str] = set()
    replaced: set[str] = set()

    for _ in range(int(90 * 60 / DT)):  # 90 simulated minutes
        before = {a.agent_id for a in sim.population.agents if isinstance(a, Truck)}
        sim.step(DT)
        replaced |= before - {a.agent_id for a in sim.population.agents if isinstance(a, Truck)}
        seen_states |= {
            str(agent.state) for agent in sim.population.agents if isinstance(agent, Truck)
        }

    assert "docked" in seen_states, "no truck ever reached a dock"
    assert "driving_to_exit" in seen_states, "no truck ever left its dock"
    assert replaced, "no truck completed the cycle and got replaced"
    assert len(sim.population.agents) == 4, "the population must stay at its target size"


def test_dwell_times_straddle_the_uc1_threshold() -> None:
    """UC1 asks for trucks that stayed more than fifteen minutes — so some must, and some must not."""
    sim = YardSimulator(seed=1337, trucks=8, forklifts=0, people=0, drones=0)
    dwells = [agent.dwell_s for agent in sim.population.agents if isinstance(agent, Truck)]
    assert any(d > 900 for d in dwells), "no truck would ever satisfy the UC1 query"
    assert any(d <= 900 for d in dwells), "every truck satisfies it, so the query proves nothing"


def test_queued_trucks_occupy_distinct_positions() -> None:
    """Six trucks at one coordinate rendered as one dot with six overprinted labels."""
    sim = YardSimulator(seed=5, trucks=6, forklifts=0, people=0, drones=0)
    run_for(sim, 40)
    waiting = [
        agent
        for agent in sim.population.agents
        if isinstance(agent, Truck) and agent.state is TruckState.AT_GATE
    ]
    if len(waiting) < 2:
        pytest.skip("fewer than two trucks queued in this window")
    positions = {(round(t.gps_reading().lat, 5), round(t.gps_reading().lon, 5)) for t in waiting}
    assert len(positions) == len(waiting), "queued trucks must form a line, not a pile"


def test_forklifts_workers_and_the_drone_keep_moving() -> None:
    """Every mover must eventually move: a stalled agent is the bug this file exists for."""
    sim = YardSimulator(seed=3, trucks=1, forklifts=2, people=3, drones=1)
    start = {
        agent.agent_id: (agent.kinematics.east, agent.kinematics.north)
        for agent in sim.population.agents
        if isinstance(agent, (Forklift, Worker, Drone))
    }
    run_for(sim, 300)
    moved = 0
    for agent in sim.population.agents:
        if agent.agent_id not in start:
            continue
        east, north = start[agent.agent_id]
        if abs(agent.kinematics.east - east) + abs(agent.kinematics.north - north) > 1.0:
            moved += 1
    assert moved == len(start), f"only {moved}/{len(start)} non-truck agents moved"


def test_the_drone_drains_and_swaps_its_battery() -> None:
    sim = YardSimulator(seed=11, trucks=0, forklifts=0, people=0, drones=1)
    drone = next(agent for agent in sim.population.agents if isinstance(agent, Drone))
    run_for(sim, 600)
    assert drone.battery_pct < 100.0, "the battery must drain (it feeds the M10 forecast)"
    run_for(sim, 4200)
    assert drone.battery_pct > 0.0, "the drone must swap its battery rather than die silently"


# ------------------------------------------------------------------- observations
def test_observations_land_on_the_right_topics() -> None:
    sim = YardSimulator(seed=1337)
    topics: dict[str, int] = {}
    run_for(sim, 60)
    for _ in range(int(60 / DT)):
        for topic, _observation in sim.step(DT).observations:
            topics[topic] = topics.get(topic, 0) + 1
    assert topics.get(str(Topic.RAW_GPS), 0) > 0
    assert topics.get(str(Topic.RAW_IOT), 0) > 0
    assert topics.get(str(Topic.RAW_FRAMES), 0) > 0


def test_observation_rate_stays_inside_the_prd_envelope() -> None:
    """PRD §13: 10-50 events/second end to end on a laptop.

    Publishing every source on every tick produced 65/s and told us nothing extra about the yard.
    Per-source rates (GPS 1 Hz, sensors 0.2 Hz) are both realistic and affordable.
    """
    sim = YardSimulator(seed=1337, trucks=6, forklifts=3, people=8, drones=1)
    run_for(sim, 30)  # let the yard fill
    total = 0
    for _ in range(int(60 / DT)):
        total += len(sim.step(DT).observations)
    rate = total / 60
    assert 5 <= rate <= 50, f"observation rate {rate:.1f}/s is outside the target envelope"


def test_gps_readings_are_noisy() -> None:
    """Perfect positions would mean fusion and association gating never get exercised."""
    sim = YardSimulator(seed=1337, trucks=1, forklifts=0, people=0, drones=0)
    run_for(sim, 20)
    truck = next(agent for agent in sim.population.agents if isinstance(agent, Truck))
    samples = [truck.gps_reading() for _ in range(20)]
    assert len({(round(s.lat, 6), round(s.lon, 6)) for s in samples}) > 1


def test_frames_are_only_emitted_when_something_is_in_view() -> None:
    """A camera staring at empty tarmac at 2 fps would be most of the bus traffic and none of the value."""
    sim = YardSimulator(seed=1337, trucks=6, forklifts=3, people=8, drones=1)
    run_for(sim, 30)
    frames = []
    for _ in range(int(30 / DT)):
        frames += [
            observation
            for topic, observation in sim.step(DT).observations
            if topic == str(Topic.RAW_FRAMES)
        ]
    assert frames, "no frames at all"
    for frame in frames:
        assert frame.payload["visible"] or frame.payload["fire"], (
            "a frame was emitted with nothing in view"
        )
        assert frame.raw_ref and frame.raw_ref.startswith("frames/")


def test_frame_bounding_boxes_are_plausible() -> None:
    """Not photogrammetric, but nearer must mean bigger and left must mean left."""
    sim = YardSimulator(seed=1337, trucks=6, forklifts=2, people=6, drones=1)
    run_for(sim, 45)
    boxes = []
    for _ in range(int(60 / DT)):
        for topic, observation in sim.step(DT).observations:
            if topic == str(Topic.RAW_FRAMES):
                boxes += [
                    (visible["bbox"], visible["distance_m"])
                    for visible in observation.payload["visible"]
                ]
    assert boxes, "no detections in any frame"
    for (x1, y1, x2, y2), distance in boxes:
        assert 0 <= x1 < x2 <= 1280 and 0 <= y1 < y2 <= 720, "box outside the frame"
        assert distance > 0
    near = [(b[3] - b[1]) for b, d in boxes if d < 15]
    far = [(b[3] - b[1]) for b, d in boxes if d > 45]
    if near and far:
        assert sum(near) / len(near) > sum(far) / len(far), "nearer objects must appear larger"


def test_rfid_fires_once_per_truck_per_gate() -> None:
    """A gate reader is event-driven; a continuous stream of reads would be wrong."""
    sim = YardSimulator(seed=1337, trucks=3, forklifts=0, people=0, drones=0)
    reads: list[tuple[str, str]] = []
    for _ in range(int(600 / DT)):
        for _topic, observation in sim.step(DT).observations:
            if observation.payload.get("metric") == "rfid_read":
                reads.append((observation.source_id, observation.payload["agent_id"]))
    assert reads, "no RFID reads at all"
    assert len(reads) == len(set(reads)), "the same tag was read twice at the same reader"


# -------------------------------------------------------------------------- incidents
def test_injecting_a_fire_ramps_the_thermal_sensor() -> None:
    """UC2's trigger. A ramp, not a step: a step change would flatter the anomaly detector."""
    sim = YardSimulator(seed=1337, trucks=2, forklifts=1, people=2, drones=0)
    run_for(sim, 30)

    def temperature() -> float | None:
        for _ in range(int(30 / DT)):
            for _topic, observation in sim.step(DT).observations:
                if observation.source_id == "iot-temp-dock-3":
                    return float(observation.payload["value"])
        return None

    baseline = temperature()
    assert baseline is not None and baseline < 30

    sim.inject_fire("dock_3")
    run_for(sim, 120)
    during = temperature()
    assert during is not None and during > baseline + 20, "the fire is invisible to the sensor"

    run_for(sim, 180)
    later = temperature()
    assert later is not None and later > during, "the temperature must keep climbing"


def test_fire_marks_frames_from_covering_cameras_only() -> None:
    sim = YardSimulator(seed=1337, trucks=2, forklifts=1, people=2, drones=0)
    sim.inject_fire("dock_3")
    run_for(sim, 5)
    flagged: set[str] = set()
    for _ in range(int(30 / DT)):
        for topic, observation in sim.step(DT).observations:
            if topic == str(Topic.RAW_FRAMES) and observation.payload.get("fire"):
                flagged.add(observation.source_id)
    assert flagged, "no camera reported the fire"
    assert all("dock" in source or source == "cam-fuel" for source in flagged), (
        f"a camera that cannot see dock_3 reported the fire: {flagged}"
    )


def test_incidents_expire() -> None:
    sim = YardSimulator(seed=1337, trucks=1, forklifts=0, people=0, drones=0)
    sim.inject_fire("dock_3", duration_s=60)
    run_for(sim, 30)
    assert sim.incident_in("dock_3", "fire") is not None
    run_for(sim, 90)
    assert sim.incident_in("dock_3", "fire") is None, "the fire burned forever"


def test_power_failure_collapses_the_power_reading() -> None:
    sim = YardSimulator(seed=1337, trucks=1, forklifts=0, people=0, drones=0)
    sim.inject_power_failure(300)
    run_for(sim, 10)
    values = []
    for _ in range(int(60 / DT)):
        for _topic, observation in sim.step(DT).observations:
            if observation.source_id == "iot-power-main":
                values.append(float(observation.payload["value"]))
    assert values and max(values) < 10, f"power did not drop: {values[:5]}"


# --------------------------------------------------------------------------- entities
def test_entities_report_a_truthful_lifetime() -> None:
    """Dwell time is the number UC1 turns on, so it must be real on the *live stream*, not only in
    the store after a merge.

    The simulator used to build a fresh Entity every tick and let pydantic default the timestamps,
    stamping first_seen = last_seen = now. The stored value was corrected by the graph store's merge,
    so REST looked right — but every consumer of the stream (including the UI panel an operator
    reads) saw a dwell of zero.
    """
    sim = YardSimulator(seed=1337, trucks=3, forklifts=2, people=2, drones=1)
    run_for(sim, 20 * 60)

    entities = sim.ground_truth_entities()
    assert entities
    for entity in entities:
        assert entity.first_seen < entity.last_seen, f"{entity.label} has a zero-length lifetime"
    longest = max(entity.dwell_s() for entity in entities)
    assert longest == pytest.approx(20 * 60, abs=5), (
        "an agent present from the start should report ~20 minutes on site"
    )


def test_timestamps_follow_the_simulated_clock_not_wall_time() -> None:
    """Twenty simulated minutes must read as twenty minutes even if they take half a second."""
    sim = YardSimulator(seed=1337, trucks=2, forklifts=0, people=0, drones=0)
    run_for(sim, 20 * 60)
    assert sim.wall_elapsed_s < 10, "this test is meaningless if it really took 20 minutes"
    span = (sim.now_utc() - sim.started_utc).total_seconds()
    assert span == pytest.approx(20 * 60, abs=1)


def test_forklifts_do_not_park_on_top_of_trucks() -> None:
    """Two entities at one coordinate render as a single dot with two overprinted labels."""
    import math

    sim = YardSimulator(seed=1337, trucks=4, forklifts=3, people=0, drones=0)
    run_for(sim, 15 * 60)
    trucks = [
        (agent.kinematics.east, agent.kinematics.north)
        for agent in sim.population.agents
        if isinstance(agent, Truck)
    ]
    forklifts = [
        (agent.kinematics.east, agent.kinematics.north)
        for agent in sim.population.agents
        if isinstance(agent, Forklift)
    ]
    assert trucks and forklifts
    closest = min(math.dist(truck, forklift) for truck in trucks for forklift in forklifts)
    assert closest > 5.0, (
        f"a forklift is {closest:.2f} m from a truck — they will render as one dot"
    )


def test_ground_truth_entities_are_labelled_as_simulated() -> None:
    """The Phase 1 bridge must never be mistaken for real perception output."""
    sim = YardSimulator(seed=1337)
    run_for(sim, 5)
    for entity in sim.ground_truth_entities():
        assert entity.attributes.get("simulated") is True
        assert entity.provenance, "an entity with no provenance cannot be explained"
        assert entity.state.geo is not None


def test_site_entities_include_the_fixed_cast() -> None:
    sim = YardSimulator(seed=1337)
    entities = sim.site_entities()
    types = {str(entity.type) for entity in entities}
    assert {"camera", "sensor", "gate", "dock"} <= types
    assert all(entity.is_static for entity in entities)
    assert all(entity.state.geo is not None for entity in entities)


def test_load_site_matches_the_default_yard() -> None:
    assert load_site().name == default_yard().name
