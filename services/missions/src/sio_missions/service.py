"""Mission control (PRD M17, Phase 6).

CRUD, objectives that complete themselves from observation, resource assignment that cannot double-book, an
append-only comms log, derived progress, and a replay window that lines up with the timeline.

The mission is the one object in this platform that a *human* owns. Events are detected, alerts are ranked,
decisions are recommended — all machine-first, with a human approving. A mission is the opposite: a person
declares an intent and the platform helps them track it. That shapes the whole service. It refuses illegal state
moves but not unusual ones; it explains rather than blocks; and every automatic act it performs writes a line in
the log, because a mission log with gaps in it is not evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from sio_core import (
    MessageContext,
    PgPool,
    SioService,
    get_pg_pool,
)
from sio_schemas import (
    BusMessage,
    Mission,
    MissionObjective,
    MissionState,
    Topic,
    new_id,
)

from .progress import evaluate, newly_completed
from .state import HOLDS_RESOURCES, TERMINAL, Refusal, check, completion_blockers


class MissionRequest(BaseModel):
    name: str
    description: str | None = None
    commander: str | None = None
    zone_id: str | None = None
    objectives: list[dict[str, Any]] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)


class ObjectiveRequest(BaseModel):
    description: str
    zone_id: str | None = None
    due_ts: datetime | None = None


class CommRequest(BaseModel):
    body: str
    kind: str = "message"
    ref: str | None = None
    author: str | None = None


class MissionsService(SioService):
    """The service behind Mission Control."""

    name = "missions"
    #: Events and alerts, so a mission can pick up what happened while it was running.
    #:
    #: Not raw topics: a mission is a human-timescale object, and attaching thirty GPS fixes a second to one
    #: would make its event list useless as a narrative.
    subscribes = (Topic.EVENTS, Topic.ALERTS)
    #: Objectives are re-evaluated on a tick as well as on events, because a resource can arrive in a zone
    #: without any event firing — zone entry is debounced by hysteresis, and an objective should not wait on a
    #: dwell threshold that exists for a different purpose.
    tick_interval_s = 5.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self._auto_completed = 0

    async def setup(self) -> None:
        await self.pool.open()
        counts = await self.pool.fetchrow(
            """
            SELECT count(*) FILTER (WHERE state = 'active') AS active,
                   count(*) FILTER (WHERE state = 'draft') AS draft,
                   count(*) AS total
              FROM missions WHERE tenant_id = %s
            """,
            (self.settings.tenant_id,),
        )
        self.log.info(
            "missions.ready", **{key: int(value or 0) for key, value in dict(counts or {}).items()}
        )

    async def health_checks(self) -> dict[str, str]:
        checks = {"postgres": "ok" if await self.pool.ping() else "unreachable"}
        orphaned = await self.pool.fetchval(
            """
            SELECT count(*) FROM mission_resources r
             WHERE r.tenant_id = %s AND r.released_ts IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM missions m
                    WHERE m.tenant_id = r.tenant_id AND m.mission_id = r.mission_id
                      AND m.state IN ('active', 'paused')
               )
            """,
            (self.settings.tenant_id,),
        )
        if int(orphaned or 0):
            # A resource still held by a mission that has ended. Reported rather than silently reclaimed:
            # the drone is genuinely unavailable to anybody else, and quietly freeing it would hide a bug in
            # the release path.
            checks["resources"] = (
                f"degraded: {orphaned} resource(s) still held by missions that are no longer running"
            )
        return checks

    async def health_info(self) -> dict[str, str]:
        return {"objectives_auto_completed": str(self._auto_completed)}

    # ------------------------------------------------------------------ live
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        """Attach what happens to the missions it concerns.

        Matched by zone, which is coarse and correct: a mission owns an area for a period, and a fire in that
        area during that period is part of its story whether or not anybody linked it. Requiring a human to
        attach events by hand produces a mission record that reflects who was watching rather than what happened.
        """
        if message.kind not in ("Event", "Alert"):
            return
        payload = message.payload if isinstance(message.payload, dict) else {}
        zone = payload.get("zone_id")
        if not zone:
            return

        rows = await self.pool.fetch(
            """
            SELECT mission_id, payload FROM missions
             WHERE tenant_id = %s AND state IN ('active', 'paused') AND zone_id = %s
            """,
            (self.settings.tenant_id, str(zone)),
        )
        for row in rows:
            key = "event_ids" if message.kind == "Event" else "alert_ids"
            stored = dict(row["payload"] or {})
            attached = list(stored.get(key) or [])
            identifier = str(payload.get("event_id") or payload.get("alert_id") or message.id)
            if identifier in attached:
                continue
            # Capped. A long-running mission in a busy zone would otherwise accumulate an unbounded list in a
            # jsonb column, and the interesting entries are the recent ones.
            attached = [*attached[-199:], identifier]
            stored[key] = attached
            await self.pool.execute(
                "UPDATE missions SET payload = %s, updated_ts = now() "
                " WHERE tenant_id = %s AND mission_id = %s",
                (self._json(stored), self.settings.tenant_id, row["mission_id"]),
            )
            if message.kind == "Alert":
                # Alerts go in the comms log; events do not. An alert is something a commander should be told
                # about, and a log that also carries every zone entry buries it.
                await self._log_comm(
                    str(row["mission_id"]),
                    author="platform",
                    kind="system",
                    body=f"Alert attached: {payload.get('title') or identifier}",
                    ref=identifier,
                )

    async def tick(self) -> None:
        """Re-evaluate objectives against where the world actually is.

        On a tick rather than only on events, because a drone can arrive in a zone without an event firing —
        zone entry is debounced by hysteresis, which exists to stop an entity on a boundary from producing a
        stream of enter/exit pairs. An objective should not inherit a threshold designed for a different problem.
        """
        rows = await self.pool.fetch(
            """
            SELECT mission_id, payload, resources FROM missions
             WHERE tenant_id = %s AND state = 'active'
            """,
            (self.settings.tenant_id,),
        )
        if not rows:
            return

        occupancy = await self._occupancy()
        for row in rows:
            stored = dict(row["payload"] or {})
            objectives = list(stored.get("objectives") or [])
            if not objectives:
                continue
            resources = tuple(str(item) for item in (row["resources"] or ()))
            updated, _ = evaluate(objectives, occupancy=occupancy, resources=resources)
            completed = newly_completed(objectives, updated)
            if not completed:
                continue

            stored["objectives"] = updated
            await self.pool.execute(
                "UPDATE missions SET payload = %s, updated_ts = now() "
                " WHERE tenant_id = %s AND mission_id = %s",
                (self._json(stored), self.settings.tenant_id, row["mission_id"]),
            )
            for objective in completed:
                self._auto_completed += 1
                by = ", ".join(objective.get("satisfied_by") or ())
                await self._log_comm(
                    str(row["mission_id"]),
                    author="platform",
                    kind="system",
                    # Names the resource that satisfied it, because "objective met" without a cause is the sort
                    # of log line that makes an incident review harder rather than easier.
                    body=f"Objective met: {objective.get('description')} — observed {by} in {objective.get('zone_id')}",
                    ref=str(objective.get("objective_id")),
                )
                self.log.info(
                    "missions.objective_met",
                    mission=row["mission_id"],
                    objective=objective.get("objective_id"),
                    satisfied_by=objective.get("satisfied_by"),
                )

    async def _occupancy(self) -> dict[str, set[str]]:
        """Which entities are in which zone, right now, from the world model.

        A window, because `zone_id` on an entity is its last known zone and this platform deletes nothing — an
        entity that left an hour ago still carries the zone it was last seen in. Without the window, an objective
        would be satisfied by a truck that has since driven to another county.
        """
        rows = await self.pool.fetch(
            """
            SELECT zone_id, entity_id FROM entities
             WHERE tenant_id = %s AND zone_id IS NOT NULL
               AND last_seen > now() - interval '2 minutes'
            """,
            (self.settings.tenant_id,),
        )
        occupancy: dict[str, set[str]] = {}
        for row in rows:
            occupancy.setdefault(str(row["zone_id"]), set()).add(str(row["entity_id"]))
        return occupancy

    # ------------------------------------------------------------ persistence
    @staticmethod
    def _json(value: Any) -> str:
        import json

        return json.dumps(value, default=str)

    async def _log_comm(
        self,
        mission_id: str,
        *,
        author: str,
        body: str,
        kind: str = "message",
        ref: str | None = None,
    ) -> str:
        comm_id = new_id("cmm")
        await self.pool.execute(
            """
            INSERT INTO mission_comms (tenant_id, comm_id, mission_id, author, kind, body, ref)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (self.settings.tenant_id, comm_id, mission_id, author, kind, body, ref),
        )
        return comm_id

    async def _load(self, mission_id: str) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            SELECT mission_id, name, description, state, commander, zone_id, assignees, resources,
                   created_ts, updated_ts, started_ts, completed_ts, payload
              FROM missions WHERE tenant_id = %s AND mission_id = %s
            """,
            (self.settings.tenant_id, mission_id),
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown mission {mission_id!r}")
        return dict(row)

    async def _render(self, row: dict[str, Any], *, include_comms: bool = False) -> dict[str, Any]:
        """One mission, with its derived facts.

        Progress is computed here and never stored: a stored percentage drifts from its objectives the moment one
        is added, and then two numbers on one screen disagree about the same mission.
        """
        stored = dict(row.get("payload") or {})
        objectives = list(stored.get("objectives") or [])
        resources = tuple(str(item) for item in (row.get("resources") or ()))
        state = MissionState(str(row["state"]))
        occupancy = await self._occupancy() if state == MissionState.ACTIVE else {}
        objectives, progress = evaluate(objectives, occupancy=occupancy, resources=resources)
        described = progress.describe()
        if state in TERMINAL and progress.outstanding:
            # A finished mission is not "waiting on" anything — it stopped. Saying otherwise is a contradiction
            # on the face of the record, and the browser review flagged a completed mission reading "33% ·
            # waiting on Get eyes on the fuel store". What is true is that it ended with objectives unmet, and
            # for an aborted or force-completed mission that is the most important fact about it.
            verb = (
                "unmet at completion" if state == MissionState.COMPLETED else "unmet when aborted"
            )
            described["summary"] = (
                f"{progress.done} of {progress.total} met; "
                f"{len(progress.outstanding)} {verb}: {', '.join(progress.outstanding)}"
            )
            described["ended_incomplete"] = True

        rendered: dict[str, Any] = {
            "mission_id": row["mission_id"],
            "name": row["name"],
            "description": row.get("description"),
            "state": str(state),
            "commander": row.get("commander"),
            "zone_id": row.get("zone_id"),
            "assignees": list(row.get("assignees") or ()),
            "resources": list(resources),
            "objectives": objectives,
            "progress": described,
            "created_ts": row.get("created_ts"),
            "updated_ts": row.get("updated_ts"),
            "started_ts": row.get("started_ts"),
            "completed_ts": row.get("completed_ts"),
            "event_ids": list(stored.get("event_ids") or ()),
            "alert_ids": list(stored.get("alert_ids") or ()),
            "legal_transitions": [str(item) for item in _legal(state)],
            "replay": self._replay_window(row),
        }
        if include_comms:
            rendered["comms"] = await self._comms(str(row["mission_id"]))
        return rendered

    def _replay_window(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """The window to hand `/api/replay`, so "replay this mission" is one click.

        A mission already defines the two things a replay needs — a start and an end — and making the operator
        copy timestamps into a replay form is the sort of gap that leaves a feature technically present and
        never used.

        `None` for a draft: there is nothing to watch, and offering a replay button that produces an empty
        stream is worse than not offering one.
        """
        started = row.get("started_ts")
        if not started:
            return None
        ended = row.get("completed_ts") or datetime.now(UTC)
        return {
            # `Z`, not `+00:00`. `isoformat()` produces the latter, and a `+` in a query string is decoded as a
            # SPACE — so pasting this window into `/api/replay?from=…` fails with "unexpected extra characters".
            # Found by doing exactly that. Handing a client a value that breaks when used the obvious way is how
            # a feature ends up technically present and never used.
            "from": _z(started),
            "to": _z(ended),
            "live": row.get("completed_ts") is None,
            "zone_id": row.get("zone_id"),
        }

    async def _comms(self, mission_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT comm_id, ts, author, kind, body, ref FROM mission_comms
             WHERE tenant_id = %s AND mission_id = %s
             ORDER BY ts LIMIT %s
            """,
            (self.settings.tenant_id, mission_id, limit),
        )
        # Ascending: a comms log is a narrative and reads forwards, unlike an alert list where the newest
        # matters most.
        return [dict(row) for row in rows]

    # -------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/missions", tags=["missions"])
        async def list_missions(
            state: str | None = None, limit: int = Query(default=50, ge=1, le=200)
        ) -> dict[str, Any]:
            rows = await self.pool.fetch(
                """
                SELECT mission_id, name, description, state, commander, zone_id, assignees, resources,
                       created_ts, updated_ts, started_ts, completed_ts, payload
                  FROM missions
                 WHERE tenant_id = %s AND (%s::text IS NULL OR state = %s)
                 ORDER BY (state IN ('active', 'paused')) DESC, updated_ts DESC
                 LIMIT %s
                """,
                (self.settings.tenant_id, state, state, limit),
            )
            # Running missions first, then by recency. A commander opening this screen during an incident wants
            # what is live, and sorting purely by time buries an active mission under drafts somebody wrote today.
            return {"missions": [await self._render(dict(row)) for row in rows]}

        @app.post("/missions", tags=["missions"])
        async def create(request: MissionRequest) -> dict[str, Any]:
            objectives = [
                MissionObjective(
                    description=str(item.get("description") or item),
                    zone_id=item.get("zone_id") if isinstance(item, dict) else None,
                ).model_dump(mode="json")
                for item in request.objectives
            ]
            mission = Mission(
                tenant_id=self.settings.tenant_id,
                name=request.name,
                description=request.description,
                commander=request.commander,
                zone_id=request.zone_id,
                assignees=request.assignees,
            )
            await self.pool.execute(
                """
                INSERT INTO missions (tenant_id, mission_id, name, description, state, commander,
                                      zone_id, assignees, resources, created_ts, updated_ts, payload)
                VALUES (%s, %s, %s, %s, 'draft', %s, %s, %s, '{}', %s, %s, %s)
                """,
                (
                    mission.tenant_id,
                    mission.mission_id,
                    mission.name,
                    mission.description,
                    mission.commander,
                    mission.zone_id,
                    mission.assignees,
                    mission.created_ts,
                    mission.updated_ts,
                    self._json({"objectives": objectives}),
                ),
            )
            await self._log_comm(
                mission.mission_id,
                author=mission.commander or "unknown",
                kind="system",
                body=f"Mission created: {mission.name}",
            )
            self.log.info(
                "missions.created",
                mission=mission.mission_id,
                name=mission.name,
                objectives=len(objectives),
                zone=mission.zone_id,
            )
            return await self._render(await self._load(mission.mission_id))

        @app.get("/missions/{mission_id}", tags=["missions"])
        async def get_mission(mission_id: str) -> dict[str, Any]:
            return await self._render(await self._load(mission_id), include_comms=True)

        @app.post("/missions/{mission_id}/state", tags=["missions"])
        async def transition(
            mission_id: str,
            to: str,
            by: str | None = None,
            force: bool = False,
        ) -> dict[str, Any]:
            """Move a mission through its lifecycle, refusing illegal moves with a reason.

            The refusal explains rather than merely denying, because "cannot go from draft to completed" is a
            fact while "a mission that never started cannot be complete — start it first, or abort it" tells the
            caller which of the two things they meant.
            """
            row = await self._load(mission_id)
            current = MissionState(str(row["state"]))
            try:
                requested = MissionState(to)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"{to!r} is not a mission state; one of: "
                    f"{', '.join(str(item) for item in MissionState)}",
                ) from None

            refusal = check(current, requested)
            if refusal is not None:
                raise HTTPException(status_code=409, detail=_refusal_detail(current, refusal))

            stored = dict(row.get("payload") or {})
            objectives = list(stored.get("objectives") or [])
            blockers: list[str] = []
            if requested == MissionState.COMPLETED:
                blockers = completion_blockers(objectives, force=force)
                if blockers:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": f"{len(blockers)} objective(s) are still open",
                            "outstanding": blockers,
                            # `force` exists because the platform does not get to tell a commander an operation
                            # is unfinished. Sometimes an objective stops being relevant. What it can insist on
                            # is that the override is recorded.
                            "fix": (
                                "complete or drop them, or pass force=true — which completes the mission and "
                                "writes a comms entry naming what was outstanding, so the review sees the "
                                "decision rather than a tidy mission"
                            ),
                        },
                    )

            stamps = ""
            if requested == MissionState.ACTIVE and not row.get("started_ts"):
                stamps = ", started_ts = now()"
            if requested in TERMINAL:
                stamps = ", completed_ts = now()"

            await self.pool.execute(
                f"UPDATE missions SET state = %s, updated_ts = now(){stamps} "
                " WHERE tenant_id = %s AND mission_id = %s",
                (str(requested), self.settings.tenant_id, mission_id),
            )

            author = by or "unknown"
            await self._log_comm(
                mission_id,
                author=author,
                kind="system",
                body=f"State: {current} → {requested}",
            )
            if force and completion_blockers(objectives):
                # The override, in the log, naming what was left. A forced completion that looks identical to a
                # clean one is how a review draws the wrong conclusion.
                outstanding = ", ".join(completion_blockers(objectives))
                await self._log_comm(
                    mission_id,
                    author=author,
                    kind="system",
                    body=f"Completed with objectives outstanding, by decision of {author}: {outstanding}",
                )

            # Resources are released when a mission stops holding them, and only then. A paused mission keeps
            # its drone — that is what pause is for.
            if requested not in HOLDS_RESOURCES:
                released = await self.pool.execute(
                    """
                    UPDATE mission_resources SET released_ts = now()
                     WHERE tenant_id = %s AND mission_id = %s AND released_ts IS NULL
                    """,
                    (self.settings.tenant_id, mission_id),
                )
                if released:
                    await self.pool.execute(
                        "UPDATE missions SET resources = '{}' WHERE tenant_id = %s AND mission_id = %s",
                        (self.settings.tenant_id, mission_id),
                    )
                    await self._log_comm(
                        mission_id,
                        author="platform",
                        kind="system",
                        body=f"{released} resource(s) released and available to other missions",
                    )

            self.log.info(
                "missions.transition",
                mission=mission_id,
                was=str(current),
                now=str(requested),
                by=author,
            )
            return await self._render(await self._load(mission_id), include_comms=True)

        @app.post("/missions/{mission_id}/resources", tags=["missions"])
        async def assign(
            mission_id: str, resource_id: str, by: str | None = None, role: str | None = None
        ) -> dict[str, Any]:
            """Commit a resource to a mission.

            The database refuses a double-booking through a partial unique index, and this handler translates
            that into an explanation. Relying on the index rather than a read-then-write check is deliberate:
            two concurrent requests both see "not assigned" and both write, and dispatching the same drone to
            two fires is exactly the failure worth making impossible rather than unlikely.
            """
            row = await self._load(mission_id)
            state = MissionState(str(row["state"]))
            if state in TERMINAL:
                raise HTTPException(
                    status_code=409,
                    detail=f"this mission is {state}; assigning a resource to it would commit the resource "
                    f"to something that is over",
                )

            holder = await self.pool.fetchrow(
                """
                SELECT r.mission_id, m.name FROM mission_resources r
                  JOIN missions m ON m.tenant_id = r.tenant_id AND m.mission_id = r.mission_id
                 WHERE r.tenant_id = %s AND r.resource_id = %s AND r.released_ts IS NULL
                """,
                (self.settings.tenant_id, resource_id),
            )
            if holder is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{resource_id} is already committed to {holder['name']!r} "
                        f"({holder['mission_id']}). Release it there first — a resource assigned to two "
                        f"missions is one that will be sent to two places."
                    ),
                )

            try:
                await self.pool.execute(
                    """
                    INSERT INTO mission_resources (tenant_id, mission_id, resource_id, assigned_by, role)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, mission_id, resource_id) DO UPDATE
                       SET released_ts = NULL, assigned_ts = now(), assigned_by = EXCLUDED.assigned_by
                    """,
                    (self.settings.tenant_id, mission_id, resource_id, by, role),
                )
            except Exception as error:
                # The index caught a race the check above could not. Reported as the same conflict, because
                # from the caller's point of view it is the same fact.
                if "mission_resources_one_mission_idx" in str(error):
                    raise HTTPException(
                        status_code=409,
                        detail=f"{resource_id} was committed to another mission a moment ago; "
                        f"reload and try again",
                    ) from error
                raise

            await self.pool.execute(
                """
                UPDATE missions SET resources = (
                    SELECT coalesce(array_agg(resource_id ORDER BY resource_id), '{}')
                      FROM mission_resources
                     WHERE tenant_id = %s AND mission_id = %s AND released_ts IS NULL
                ), updated_ts = now()
                 WHERE tenant_id = %s AND mission_id = %s
                """,
                (self.settings.tenant_id, mission_id, self.settings.tenant_id, mission_id),
            )
            await self._log_comm(
                mission_id,
                author=by or "unknown",
                kind="system",
                body=f"{resource_id} assigned{f' as {role}' if role else ''}",
                ref=resource_id,
            )
            self.log.info("missions.assigned", mission=mission_id, resource=resource_id, by=by)
            return await self._render(await self._load(mission_id))

        @app.delete("/missions/{mission_id}/resources/{resource_id}", tags=["missions"])
        async def release(
            mission_id: str, resource_id: str, by: str | None = None
        ) -> dict[str, Any]:
            released = await self.pool.execute(
                """
                UPDATE mission_resources SET released_ts = now()
                 WHERE tenant_id = %s AND mission_id = %s AND resource_id = %s AND released_ts IS NULL
                """,
                (self.settings.tenant_id, mission_id, resource_id),
            )
            if not released:
                raise HTTPException(
                    status_code=404,
                    detail=f"{resource_id} is not currently assigned to {mission_id}",
                )
            await self.pool.execute(
                """
                UPDATE missions SET resources = (
                    SELECT coalesce(array_agg(resource_id ORDER BY resource_id), '{}')
                      FROM mission_resources
                     WHERE tenant_id = %s AND mission_id = %s AND released_ts IS NULL
                ), updated_ts = now()
                 WHERE tenant_id = %s AND mission_id = %s
                """,
                (self.settings.tenant_id, mission_id, self.settings.tenant_id, mission_id),
            )
            await self._log_comm(
                mission_id,
                author=by or "unknown",
                kind="system",
                body=f"{resource_id} released",
                ref=resource_id,
            )
            return await self._render(await self._load(mission_id))

        @app.post("/missions/{mission_id}/objectives", tags=["missions"])
        async def add_objective(mission_id: str, request: ObjectiveRequest) -> dict[str, Any]:
            row = await self._load(mission_id)
            if MissionState(str(row["state"])) in TERMINAL:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": f"this mission is {row['state']}; adding an objective to it would change "
                        f"a record of what happened",
                        "fix": "open a new mission",
                    },
                )
            stored = dict(row.get("payload") or {})
            objectives = list(stored.get("objectives") or [])
            objective = MissionObjective(
                description=request.description,
                zone_id=request.zone_id,
                due_ts=request.due_ts,
            )
            objectives.append(objective.model_dump(mode="json"))
            stored["objectives"] = objectives
            await self.pool.execute(
                "UPDATE missions SET payload = %s, updated_ts = now() "
                " WHERE tenant_id = %s AND mission_id = %s",
                (self._json(stored), self.settings.tenant_id, mission_id),
            )
            await self._log_comm(
                mission_id,
                author="unknown",
                kind="system",
                body=f"Objective added: {objective.description}"
                + (
                    f" (auto-completes when an assigned resource reaches {objective.zone_id})"
                    if objective.zone_id
                    else ""
                ),
                ref=objective.objective_id,
            )
            return await self._render(await self._load(mission_id))

        @app.post("/missions/{mission_id}/objectives/{objective_id}", tags=["missions"])
        async def complete_objective(
            mission_id: str, objective_id: str, done: bool = True, by: str | None = None
        ) -> dict[str, Any]:
            """Tick an objective by hand.

            Still needed despite auto-completion: an objective with no zone has nothing to observe, and a human
            judgement — "the area is safe" — is not something the platform can verify. Being explicit about which
            objectives it can and cannot check is more useful than pretending uniformity.
            """
            row = await self._load(mission_id)
            state = MissionState(str(row["state"]))
            if state in TERMINAL:
                # "Final" and "editable" cannot both be true. A browser review toggled an objective on a
                # completed mission three times and every write was accepted, swinging a finished record
                # between 50% and 100% — so the mission's own history depended on who clicked last. The
                # transition endpoint already refuses to leave a terminal state; this is the same rule applied
                # to the mission's contents.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": f"this mission is {state}, so its objectives are a record rather than a "
                        f"list of work",
                        "fix": "open a new mission if there is more to do",
                        "state": str(state),
                    },
                )
            stored = dict(row.get("payload") or {})
            objectives = list(stored.get("objectives") or [])
            found = next(
                (item for item in objectives if str(item.get("objective_id")) == objective_id), None
            )
            if found is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"unknown objective {objective_id!r}; have "
                    f"{[str(item.get('objective_id')) for item in objectives]}",
                )
            found["done"] = done
            found["progress"] = 1.0 if done else 0.0
            if done:
                found["satisfied_by"] = [by or "a human"]
            else:
                found.pop("satisfied_by", None)
            stored["objectives"] = objectives
            await self.pool.execute(
                "UPDATE missions SET payload = %s, updated_ts = now() "
                " WHERE tenant_id = %s AND mission_id = %s",
                (self._json(stored), self.settings.tenant_id, mission_id),
            )
            await self._log_comm(
                mission_id,
                author=by or "unknown",
                kind="system",
                body=f"Objective {'met' if done else 'reopened'}: {found.get('description')}",
                ref=objective_id,
            )
            return await self._render(await self._load(mission_id), include_comms=True)

        @app.post("/missions/{mission_id}/comms", tags=["missions"])
        async def add_comm(mission_id: str, request: CommRequest) -> dict[str, Any]:
            """Append to the comms log. Append is the only verb it has.

            The table refuses UPDATE and DELETE at the database level. A comms entry is testimony — somebody said
            a thing at a time — and testimony that can be edited afterwards is worth nothing in the review that
            follows a bad outcome.
            """
            # Deliberately NOT refused on a terminal mission, unlike objectives. A debrief is written after
            # the operation ends, and a log that closes when the mission does would push the most considered
            # entries — the ones written with hindsight — somewhere else entirely.
            await self._load(mission_id)
            comm_id = await self._log_comm(
                mission_id,
                author=request.author or "unknown",
                kind=request.kind,
                body=request.body,
                ref=request.ref,
            )
            return {
                "comm_id": comm_id,
                "mission_id": mission_id,
                "note": "appended; this log cannot be edited or deleted, by design",
                "comms": await self._comms(mission_id),
            }

        @app.get("/missions/{mission_id}/comms", tags=["missions"])
        async def get_comms(mission_id: str) -> dict[str, Any]:
            await self._load(mission_id)
            return {"mission_id": mission_id, "comms": await self._comms(mission_id)}

        @app.get("/missions/{mission_id}/replay", tags=["missions"])
        async def replay_plan(mission_id: str) -> dict[str, Any]:
            """The replay window for this mission, ready to hand to `/api/replay`.

            A mission already defines the two things a replay needs. Making an operator copy timestamps into a
            separate form is the sort of gap that leaves a feature technically present and never used.
            """
            row = await self._load(mission_id)
            window = self._replay_window(row)
            if window is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{row['name']!r} has not started, so there is nothing to replay. A replay of a "
                        f"draft would be an empty stream, which is worse than no button at all."
                    ),
                )
            from urllib.parse import urlencode

            query = urlencode({"from": window["from"], "to": window["to"], "speed": 20})
            return {
                "mission_id": mission_id,
                "name": row["name"],
                **window,
                # The finished path, properly encoded. A "hint" that says to build a URL leaves every client to
                # rediscover that `+` needs escaping — which is the bug this endpoint just had.
                "replay_url": f"/api/replay?{query}",
                "hint": "POST the replay_url to plan the frames, then stream /api/replay/{replay_id}/stream",
            }


def _z(moment: Any) -> str:
    """An ISO timestamp that survives a query string.

    `datetime.isoformat()` ends in `+00:00`, and `+` means space when a URL is decoded. The `Z` form is both
    shorter and unambiguous, and every ISO 8601 parser accepts it.
    """
    if not hasattr(moment, "isoformat"):
        return str(moment)
    return moment.isoformat().replace("+00:00", "Z")


def _legal(state: MissionState) -> tuple[MissionState, ...]:
    from .state import TRANSITIONS

    return TRANSITIONS.get(state, ())


def _refusal_detail(current: MissionState, refusal: Refusal) -> dict[str, Any]:
    return {
        "message": refusal.message,
        "fix": refusal.fix,
        "state": str(current),
        "legal_transitions": list(refusal.legal),
    }


__all__ = ["MissionsService"]
