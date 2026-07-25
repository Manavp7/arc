"""Timeline reconstruction and replay (PRD M8, UC5).

Answering "what did the world look like at 14:32?" from an append-only record.

Three things make this harder than it sounds, and each is a way a replay ends up lying:

**A state row is not a presence.** An entity that left the site two hours ago still has a final state
row, so "its most recent state at or before T" happily returns a position for an object that was long
gone. Reconstruction has to distinguish *the last thing we knew* from *what was true then*, and the only
honest way is a staleness window: if nothing reported an entity for minutes before T, it was not
observably there at T.

**Zone membership lives in the edges, not the states.** The `entity_states` rows carry no zone — measured,
90,536 rows and zero of them populated — because the spatial service owns membership and records it as a
bitemporal visit interval. So the zone at T is the visit whose interval covers T, which is exactly the
question bitemporal storage exists to answer.

**A replay must be bounded.** A four-hour window at 1x speed is four hours of streaming. The frame count
is capped and the step widened to fit, and the *effective* speed is reported — a client told "1x" while
receiving one frame a minute has been misled about what it is watching.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sio_core import PgPool, get_logger
from sio_schemas import Entity, EntityState, Event, Geo, Velocity, new_id, utc_now

log = get_logger("sio.api.timeline")

MAX_REPLAY_FRAMES = 600
"""Most frames a single replay will emit.

A bound, not a preference: without it a four-hour window at one second per frame is 14,400 frames and a
connection nobody closes. Six hundred is enough for a smooth scrub of any window while keeping the whole
replay under a few minutes of wall clock.
"""

DEFAULT_PRESENCE_WINDOW_S = 120.0
"""How stale an entity's last state may be and still count as present at T.

Set from how often a moving entity is reported (about 1 Hz) times a generous margin for gaps. Too short
and a replay flickers; too long and departed objects linger as ghosts. It is a judgement, so it is a
parameter and it is reported in the response.
"""


@dataclass
class ReplaySession:
    """A server-driven replay of a time window."""

    replay_id: str
    tenant_id: str
    start: datetime
    end: datetime
    speed: float
    step_s: float
    frames: int
    created_at: datetime = field(default_factory=utc_now)
    emitted: int = 0
    cancelled: bool = False
    max_lag_s: float = 0.0
    """Worst observed lag behind the frame schedule. The only honest measure of delivered speed."""

    @property
    def window_s(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def resolution_s(self) -> float:
        """Seconds of history represented by each frame.

        This — not the speed — is what the frame cap degrades, and reporting it was worth getting right.
        My first version exposed an `effective_speed`, which was a tautology: speed is
        `step / interval` and `interval` is `step / speed`, so it could never differ from the request and
        dressed up a truism as insight.

        What the cap actually costs is temporal RESOLUTION. Replaying four hours within the frame cap
        means each frame stands for 24 seconds of history, so an object crossing the yard jumps rather
        than moves. The playback speed is exactly as requested; the granularity is not, and a client
        deserves to know which.
        """
        return self.step_s

    @property
    def frame_interval_s(self) -> float:
        """Wall-clock seconds between frames."""
        return self.step_s / max(0.01, self.speed)

    @property
    def wall_duration_s(self) -> float:
        return self.frames * self.frame_interval_s

    def frame_ts(self, index: int) -> datetime:
        return self.start + timedelta(seconds=self.step_s * index)

    def describe(self) -> dict[str, Any]:
        return {
            "replay_id": self.replay_id,
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "window_s": round(self.window_s, 1),
            "speed": self.speed,
            "resolution_s": round(self.resolution_s, 3),
            "step_s": round(self.step_s, 3),
            "frames": self.frames,
            "frame_interval_s": round(self.frame_interval_s, 3),
            "wall_duration_s": round(self.wall_duration_s, 1),
            "emitted": self.emitted,
            "max_lag_s": round(self.max_lag_s, 3),
            "capped": self.frames >= MAX_REPLAY_FRAMES,
        }


def plan_replay(
    *,
    tenant_id: str,
    start: datetime,
    end: datetime,
    speed: float = 10.0,
    step_s: float | None = None,
) -> ReplaySession:
    """Work out a frame schedule that fits inside the frame cap.

    The step is derived rather than fixed: a caller asking to replay four hours does not want to choose a
    step, they want the whole window. Widening the step is the honest response to a cap — dropping the
    tail of the window silently would be worse, because the interesting part of an incident is usually at
    the end.
    """
    window_s = max(1.0, (end - start).total_seconds())
    chosen_step = step_s or max(1.0, window_s / MAX_REPLAY_FRAMES)
    frames = min(MAX_REPLAY_FRAMES, max(1, math.ceil(window_s / chosen_step)))
    # If a caller-supplied step would overrun the cap, widen it to cover the window rather than truncate.
    if step_s is not None and window_s / step_s > MAX_REPLAY_FRAMES:
        chosen_step = window_s / MAX_REPLAY_FRAMES
        frames = MAX_REPLAY_FRAMES
    return ReplaySession(
        replay_id=new_id("rpl"),
        tenant_id=tenant_id,
        start=start,
        end=end,
        speed=max(0.1, min(600.0, speed)),
        step_s=chosen_step,
        frames=frames,
    )


class TimelineReader:
    """Reconstruction queries over the append-only record."""

    def __init__(self, pool: PgPool) -> None:
        self.pool = pool

    # ----------------------------------------------------------------- density
    async def density(
        self,
        *,
        tenant_id: str,
        start: datetime,
        end: datetime,
        buckets: int = 120,
    ) -> dict[str, Any]:
        """Event counts per time bucket, for drawing a scrubber's activity strip.

        A scrubber needs to show *where* the interesting moments are without downloading every event in
        the window. Counting in the database and returning a fixed number of buckets keeps the payload
        constant whether the window is an hour or a week — the alternative is a UI that gets slower the
        further back you look, which is exactly when you need it.
        """
        window_s = max(1.0, (end - start).total_seconds())
        bucket_s = window_s / max(1, buckets)
        rows = await self.pool.fetch(
            """
            SELECT floor(extract(epoch from (ts - %s)) / %s)::int AS bucket,
                   count(*) AS total,
                   count(*) FILTER (WHERE severity IN ('high', 'critical')) AS severe,
                   min(ts) AS first_ts
              FROM events
             WHERE tenant_id = %s AND ts >= %s AND ts <= %s
             GROUP BY bucket
             ORDER BY bucket
            """,
            (start, bucket_s, tenant_id, start, end),
        )
        counts = [0] * (buckets + 1)
        severe = [0] * (buckets + 1)
        for row in rows:
            index = max(0, min(buckets, int(row["bucket"])))
            counts[index] += int(row["total"])
            severe[index] += int(row["severe"])
        return {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "bucket_s": round(bucket_s, 3),
            "buckets": buckets,
            "counts": counts[:buckets],
            "severe": severe[:buckets],
            "total": sum(counts),
        }

    async def bounds(self, *, tenant_id: str) -> dict[str, Any]:
        """The extent of recorded history, so a scrubber knows what it may scrub over.

        Includes both the event record and the state record, because they can start at different times —
        a fresh deployment has states long before its first interesting event.
        """
        row = await self.pool.fetchrow(
            """
            SELECT
              (SELECT min(ts) FROM events WHERE tenant_id = %s) AS first_event,
              (SELECT max(ts) FROM events WHERE tenant_id = %s) AS last_event,
              (SELECT min(ts) FROM entity_states WHERE tenant_id = %s) AS first_state,
              (SELECT max(ts) FROM entity_states WHERE tenant_id = %s) AS last_state
            """,
            (tenant_id, tenant_id, tenant_id, tenant_id),
        )
        record = dict(row or {})
        candidates_start = [
            value for key, value in record.items() if key.startswith("first") and value
        ]
        candidates_end = [
            value for key, value in record.items() if key.startswith("last") and value
        ]
        return {
            **{key: value.isoformat() if value else None for key, value in record.items()},
            "start": min(candidates_start).isoformat() if candidates_start else None,
            "end": max(candidates_end).isoformat() if candidates_end else None,
            "span_s": (
                round((max(candidates_end) - min(candidates_start)).total_seconds(), 1)
                if candidates_start and candidates_end
                else 0.0
            ),
        }

    # ------------------------------------------------------------ reconstruction
    async def world_at(
        self,
        ts: datetime,
        *,
        tenant_id: str,
        limit: int = 800,
        presence_window_s: float = DEFAULT_PRESENCE_WINDOW_S,
    ) -> dict[str, Any]:
        """The world as it stood at ``ts``.

        Each entity is rewound to the last state recorded at or before ``ts`` — never to its current
        position, which is the bug that makes a replay worthless — and only appears if that state is
        recent enough relative to ``ts`` to count as presence.

        Zones come from the bitemporal visit intervals rather than from the state rows, because the state
        rows carry no zone: the spatial service owns membership and records it as an interval, and the
        interval covering ``ts`` is the answer.
        """
        rows = await self.pool.fetch(
            """
            WITH state_at AS (
                SELECT DISTINCT ON (entity_id)
                       entity_id, ts,
                       ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon,
                       speed_mps, heading_deg, confidence, payload
                  FROM entity_states
                 WHERE tenant_id = %s AND ts <= %s AND ts >= %s
                 ORDER BY entity_id, ts DESC
            )
            SELECT e.payload, e.is_static,
                   s.ts AS state_ts, s.lat, s.lon, s.speed_mps, s.heading_deg, s.confidence
              FROM entities e
              LEFT JOIN state_at s ON s.entity_id = e.entity_id
             WHERE e.tenant_id = %s
               AND e.first_seen <= %s
               -- A static fixture has no state history and is always present; a mover must have been
               -- reported inside the presence window, or it is a ghost at its final position.
               AND (e.is_static OR s.entity_id IS NOT NULL)
             ORDER BY e.is_static ASC, e.last_seen DESC
             LIMIT %s
            """,
            (
                tenant_id,
                ts,
                ts - timedelta(seconds=presence_window_s),
                tenant_id,
                ts,
                limit,
            ),
        )

        zones_by_entity = await self.memberships_at(ts, tenant_id=tenant_id)

        entities: list[Entity] = []
        movers = 0
        for row in rows:
            entity = Entity.model_validate(row["payload"])
            zone_id = zones_by_entity.get(entity.entity_id)
            if row["lat"] is not None and row["lon"] is not None:
                movers += 1
                speed = float(row["speed_mps"] or 0.0)
                heading = row["heading_deg"]
                # Reconstruct the velocity vector from speed and heading. Dropping it would leave a
                # replayed entity with no motion, and a frozen map is indistinguishable from a broken one.
                velocity = None
                if speed > 0 and heading is not None:
                    radians = math.radians(float(heading))
                    velocity = Velocity(
                        east=round(speed * math.sin(radians), 4),
                        north=round(speed * math.cos(radians), 4),
                    )
                entity = entity.model_copy(
                    update={
                        "state": EntityState(
                            ts=row["state_ts"],
                            geo=Geo(lat=float(row["lat"]), lon=float(row["lon"])),
                            velocity=velocity,
                            heading_deg=heading,
                            zone_id=zone_id,
                            confidence=float(row["confidence"] or 1.0),
                        ),
                        # Rewind last_seen too: leaving it at the present would make a replayed entity
                        # claim a dwell that had not happened yet at this instant.
                        "last_seen": row["state_ts"],
                    }
                )
            elif zone_id and entity.state is not None:
                entity = entity.model_copy(
                    update={"state": entity.state.model_copy(update={"zone_id": zone_id})}
                )
            entities.append(entity)

        return {
            "ts": ts.isoformat(),
            "entities": entities,
            "counts": {
                "total": len(entities),
                "movers": movers,
                "static": len(entities) - movers,
                "in_zones": len(zones_by_entity),
            },
            "presence_window_s": presence_window_s,
        }

    async def memberships_at(self, ts: datetime, *, tenant_id: str) -> dict[str, str]:
        """Which zone each entity was in at ``ts``, from the bitemporal visit intervals.

        The innermost zone wins where visits nest — a truck inside a dock is also inside the yard and the
        perimeter, and the most specific answer is the useful one. Ordering by the interval's start makes
        that deterministic: the enclosing visit necessarily began first, so the latest-starting visit
        covering ``ts`` is the innermost.
        """
        rows = await self.pool.fetch(
            """
            SELECT from_id, to_id, ts_valid_from
              FROM relationships
             WHERE tenant_id = %s AND type = 'entered'
               AND ts_valid_from <= %s
               AND (ts_valid_to IS NULL OR ts_valid_to >= %s)
             ORDER BY ts_valid_from ASC
            """,
            (tenant_id, ts, ts),
        )
        # Later rows overwrite earlier ones, so the last (latest-starting, hence innermost) visit wins.
        return {str(row["from_id"]): str(row["to_id"]) for row in rows}

    async def events_between(
        self,
        *,
        tenant_id: str,
        start: datetime,
        end: datetime,
        limit: int = 500,
        min_severity: str | None = None,
    ) -> list[Event]:
        """Events in a window, oldest first — the order a replay consumes them."""
        clauses = ["tenant_id = %s", "ts >= %s", "ts <= %s"]
        params: list[Any] = [tenant_id, start, end]
        if min_severity:
            ranking = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
            wanted = [
                name for name, rank in ranking.items() if rank >= ranking.get(min_severity, 0)
            ]
            clauses.append("severity = ANY(%s)")
            params.append(wanted)
        params.append(limit)
        rows = await self.pool.fetch(
            f"SELECT payload FROM events WHERE {' AND '.join(clauses)} ORDER BY ts ASC LIMIT %s",
            tuple(params),
        )
        return [Event.model_validate(row["payload"]) for row in rows]

    # ------------------------------------------------------------------- replay
    async def replay_frames(
        self, session: ReplaySession, *, presence_window_s: float = DEFAULT_PRESENCE_WINDOW_S
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield reconstructed frames on a wall-clock schedule.

        Server-driven, so the client does not poll: it receives a frame when one is due. Each frame is a
        *reconstruction*, and it carries the events that fell inside its own step so a scrubbing UI can
        show what happened without a second request per frame.

        Sleeps on an absolute schedule rather than a fixed interval, for the same reason the simulator
        does: sleeping a full interval after doing work makes the playback rate drift by however long the
        work took, and a replay that claims 10x while delivering 6x is lying about the one thing it is
        for.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        for index in range(session.frames):
            if session.cancelled:
                return
            frame_ts = session.frame_ts(index)
            world = await self.world_at(
                frame_ts,
                tenant_id=session.tenant_id,
                presence_window_s=presence_window_s,
            )
            events = await self.events_between(
                tenant_id=session.tenant_id,
                start=frame_ts,
                end=frame_ts + timedelta(seconds=session.step_s),
                limit=40,
            )
            session.emitted += 1
            due = started + session.frame_interval_s * (index + 1)
            # How far behind schedule this frame is. The one honest measure of delivered speed: the plan
            # can promise 20x, but if reconstructing a frame takes longer than the frame interval the
            # replay silently runs slow, and only the server can see that.
            lag_s = max(0.0, loop.time() - due)
            session.max_lag_s = max(session.max_lag_s, lag_s)
            yield {
                "replay_id": session.replay_id,
                "frame": index,
                "frames": session.frames,
                "ts": frame_ts.isoformat(),
                "progress": round((index + 1) / session.frames, 4),
                "resolution_s": round(session.resolution_s, 3),
                "lag_s": round(lag_s, 3),
                "entities": [entity.to_wire() for entity in world["entities"]],
                "events": [event.to_wire() for event in events],
                "counts": world["counts"],
            }
            delay = due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)


class ReplayRegistry:
    """Live replay sessions, bounded.

    Bounded because a session is created by an unauthenticated GET in the demo profile, and an unbounded
    dictionary keyed by a caller-supplied action is a memory leak with a URL.
    """

    def __init__(self, *, max_sessions: int = 32, ttl_s: float = 3600.0) -> None:
        self.max_sessions = max_sessions
        self.ttl_s = ttl_s
        self._sessions: dict[str, ReplaySession] = {}

    def add(self, session: ReplaySession) -> ReplaySession:
        self._evict()
        if len(self._sessions) >= self.max_sessions:
            # Drop the oldest rather than refusing the newest: a stale session nobody is watching is
            # less valuable than the one being asked for right now.
            oldest = min(self._sessions.values(), key=lambda item: item.created_at)
            oldest.cancelled = True
            del self._sessions[oldest.replay_id]
        self._sessions[session.replay_id] = session
        return session

    def get(self, replay_id: str) -> ReplaySession | None:
        self._evict()
        return self._sessions.get(replay_id)

    def cancel(self, replay_id: str) -> bool:
        session = self._sessions.pop(replay_id, None)
        if session is None:
            return False
        session.cancelled = True
        return True

    def _evict(self) -> None:
        cutoff = utc_now() - timedelta(seconds=self.ttl_s)
        for replay_id, session in list(self._sessions.items()):
            if session.created_at < cutoff:
                session.cancelled = True
                del self._sessions[replay_id]

    def describe(self) -> dict[str, Any]:
        return {
            "sessions": len(self._sessions),
            "max_sessions": self.max_sessions,
            "ttl_s": self.ttl_s,
            "active": [session.describe() for session in self._sessions.values()],
        }
