"""Tests for timeline reconstruction and replay (PRD M8, UC5).

The acceptance criterion is that scrubbing to T reconstructs the world *as it was at T*. The
reconstruction itself needs a database, so that is asserted in the infra suite; what is testable here is
everything around it, and the interesting parts are the refusals:

* a replay must be **bounded** — a four-hour window at 1x is four hours of streaming;
* it must report the speed it will **actually** deliver, not the one that was asked for;
* the session registry must not grow without limit, because a session is created by a request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sio_api.timeline import (
    DEFAULT_PRESENCE_WINDOW_S,
    MAX_REPLAY_FRAMES,
    ReplayRegistry,
    plan_replay,
)

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def a_session(*, minutes: float = 10.0, speed: float = 10.0, step_s: float | None = None):
    return plan_replay(
        tenant_id="acme",
        start=START,
        end=START + timedelta(minutes=minutes),
        speed=speed,
        step_s=step_s,
    )


# ------------------------------------------------------------------- planning
def test_a_replay_plan_covers_the_whole_window() -> None:
    session = a_session(minutes=10, step_s=5.0)
    assert session.frame_ts(0) == START
    assert session.frame_ts(session.frames - 1) <= session.end
    # The last frame should be within one step of the end: the window is covered, not truncated.
    assert (session.end - session.frame_ts(session.frames - 1)).total_seconds() <= session.step_s


def test_a_long_window_widens_the_step_rather_than_dropping_the_tail() -> None:
    """The interesting part of an incident is usually at the end, so truncating the window silently
    would remove exactly the part someone is replaying to see."""
    session = a_session(minutes=240, step_s=1.0)  # 14,400 frames if honoured literally
    assert session.frames <= MAX_REPLAY_FRAMES
    assert session.step_s > 1.0, "the step must widen to fit the cap"
    assert (
        session.end - session.frame_ts(session.frames - 1)
    ).total_seconds() <= session.step_s * 1.5


def test_the_frame_count_is_capped_without_a_step() -> None:
    for minutes in (1, 10, 60, 600):
        session = a_session(minutes=minutes)
        assert 1 <= session.frames <= MAX_REPLAY_FRAMES, (
            f"{minutes} min gave {session.frames} frames"
        )


def test_the_cap_degrades_resolution_not_speed_and_says_so() -> None:
    """What the frame cap costs is temporal RESOLUTION, not playback speed.

    My first version of this reported an "effective_speed", which was a tautology — speed is
    step/interval and interval is step/speed, so it could never differ from the request. It dressed up a
    truism as insight. Replaying four hours within the cap means each frame stands for tens of seconds of
    history, so an object crossing the yard jumps rather than moves; the SPEED is exactly as asked.
    """
    fine = a_session(minutes=10, speed=10.0, step_s=5.0)
    assert fine.resolution_s == 5.0
    assert fine.describe()["capped"] is False

    coarse = a_session(minutes=600, speed=10.0)
    assert coarse.describe()["capped"] is True
    assert coarse.resolution_s > fine.resolution_s * 5, (
        "the cap must be visible as coarser resolution"
    )
    assert coarse.speed == 10.0, "and the requested speed is still honoured"
    # A 10-hour window at 10x is exactly one hour of wall clock, which is what was asked for. The cap
    # bounds FRAMES, not patience — and the plan discloses wall_duration_s up front so a caller can see
    # what they have requested before the stream starts.
    assert coarse.wall_duration_s <= 3600


def test_lag_is_the_only_honest_measure_of_delivered_speed() -> None:
    """The plan can promise 20x, but if reconstructing a frame takes longer than the frame interval the
    replay silently runs slow — and only the server can see that."""
    session = a_session(minutes=10, speed=10.0, step_s=5.0)
    assert session.describe()["max_lag_s"] == 0.0
    session.max_lag_s = 2.5
    assert session.describe()["max_lag_s"] == 2.5


def test_the_wall_clock_duration_follows_the_requested_speed() -> None:
    slow = a_session(minutes=10, speed=1.0, step_s=10.0)
    fast = a_session(minutes=10, speed=20.0, step_s=10.0)
    assert slow.wall_duration_s > fast.wall_duration_s * 10


def test_speed_is_clamped_to_something_sane() -> None:
    assert a_session(speed=0.0).speed > 0, "a zero-speed replay would never finish"
    assert a_session(speed=100_000.0).speed <= 600.0


def test_a_plan_describes_itself_completely() -> None:
    description = a_session(minutes=30, speed=15.0).describe()
    for key in (
        "replay_id",
        "from",
        "to",
        "window_s",
        "speed",
        "resolution_s",
        "max_lag_s",
        "step_s",
        "frames",
        "frame_interval_s",
        "wall_duration_s",
        "capped",
    ):
        assert key in description, f"missing {key} from the plan"


# ------------------------------------------------------------------- registry
def test_the_registry_evicts_the_oldest_when_full() -> None:
    """A session is created by a request, so an unbounded dictionary keyed by a caller's action is a
    memory leak with a URL. Dropping the oldest beats refusing the newest: a stale session nobody is
    watching is worth less than the one being asked for right now.
    """
    registry = ReplayRegistry(max_sessions=3)
    sessions = []
    # Recent creation times: START is a fixed date in the past, and sessions dated there would be
    # expired by the TTL before the eviction path was ever reached.
    now = datetime.now(UTC)
    for index in range(5):
        session = a_session(minutes=1)
        session.created_at = now + timedelta(seconds=index)
        registry.add(session)
        sessions.append(session)

    assert registry.describe()["sessions"] == 3
    assert registry.get(sessions[0].replay_id) is None, "the oldest was evicted"
    assert registry.get(sessions[-1].replay_id) is not None, "the newest survives"
    assert sessions[0].cancelled, "and an evicted session is cancelled, so its loop stops"


def test_the_registry_expires_stale_sessions() -> None:
    registry = ReplayRegistry(ttl_s=60.0)
    stale = a_session()
    stale.created_at = datetime.now(UTC) - timedelta(hours=2)
    registry.add(stale)
    assert registry.get(stale.replay_id) is None
    assert stale.cancelled


def test_cancelling_a_session_removes_it() -> None:
    registry = ReplayRegistry()
    session = registry.add(a_session())
    assert registry.cancel(session.replay_id) is True
    assert session.cancelled
    assert registry.get(session.replay_id) is None
    assert registry.cancel("rpl_nonexistent") is False


def test_the_presence_window_default_is_a_stated_judgement() -> None:
    """Too short and a replay flickers; too long and departed objects linger as ghosts. It is a
    judgement, so it is a named constant with a reason and a parameter on the endpoint."""
    assert 30.0 <= DEFAULT_PRESENCE_WINDOW_S <= 600.0
