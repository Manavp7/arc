"""Decision service: recommendations with ranked options and an approval gate (PRD M12).

**Nothing here executes anything.** A decision is a proposal with `approval=pending`, and the only path to
action is a human calling `/decisions/{id}/approve`. That is the design the PRD calls human-on-the-loop, and
it is enforced structurally rather than by convention: this service has no actuator and no workflow client,
so there is no code path from "recommended" to "done" for it to take by accident.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from sio_core import MessageContext, PgPool, SioService, get_llm, get_pg_pool
from sio_schemas import (
    ApprovalState,
    BusMessage,
    Decision,
    Event,
    Severity,
    Topic,
    utc_now,
)

from .recommend import build_decision, build_options, llm_rationale, template_rationale
from .solvers import Incident, Responder

#: Event types worth recommending against. Everything else is noise for this purpose — a zone entry is not
#: a situation, and generating a recommendation for one would bury the recommendations that matter.
ACTIONABLE = {
    "fire_detected": "fire",
    "smoke_detected": "fire",
    "unauthorized_entry": "intrusion",
    "zone_breach": "intrusion",
    "person_fell": "medical",
    "congestion": "congestion",
    "dwell_exceeded": "dwell",
}

#: Entity types that count as dispatchable, and what kind of responder they are.
#:
#: `person` is deliberately ABSENT. It was there, and a live recommendation offered "Person 32Q4NH" as a
#: responder to a fire — a worker walking to their van is not a first responder, and an optimiser given the
#: whole workforce will confidently dispatch a stranger. Being on site is not the same as being available.
#:
#: A person becomes dispatchable by being MARKED as one (see RESPONDER_ROLE_ATTRIBUTE), which is a fact
#: somebody has to assert rather than one inferred from having legs.
RESPONDER_KINDS = {"drone": "drone", "forklift": "patrol"}

RESPONDER_ROLE_ATTRIBUTE = "role"
#: Attribute values that opt an entity into the responder pool regardless of its type.
RESPONDER_ROLES = {"patrol", "security", "responder", "marshal"}


class ApprovalRequest(BaseModel):
    option_id: str | None = None
    approved_by: str = "operator"
    note: str | None = None


class RejectionRequest(BaseModel):
    rejected_by: str = "operator"
    reason: str | None = None


class DecisionService(SioService):
    """Recommends, ranks, explains — and waits."""

    name = "decision"
    subscribes = (Topic.EVENTS,)
    tick_interval_s = 120.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self.llm = get_llm(self.settings) if self.settings.decision_use_llm else None
        self._proposed = 0
        self._approved = 0
        self._rejected = 0
        self._skipped = 0
        self._recent: list[Decision] = []

    async def setup(self) -> None:
        await self.pool.open()
        self.log.info(
            "decision.ready",
            solver="ortools-cpsat",
            rationale="llm" if self.llm else "template",
            actionable=sorted(ACTIONABLE),
        )

    async def health_checks(self) -> dict[str, str]:
        return {"postgres": "ok" if await self.pool.ping() else "unreachable"}

    async def health_info(self) -> dict[str, str]:
        return {
            "proposed": str(self._proposed),
            "approved": str(self._approved),
            "rejected": str(self._rejected),
            "skipped_not_actionable": str(self._skipped),
            "pending": str(sum(1 for d in self._recent if d.approval == ApprovalState.PENDING)),
        }

    # ------------------------------------------------------------------ handling
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        if message.kind != "Event":
            return
        event = message.decode(Event)
        kind = ACTIONABLE.get(str(event.type))
        if kind is None:
            self._skipped += 1
            return
        if event.severity not in (Severity.HIGH, Severity.CRITICAL):
            self._skipped += 1
            return
        await self.recommend_for(event, kind, ctx)

    async def recommend_for(
        self, event: Event, kind: str, ctx: MessageContext | None
    ) -> Decision | None:
        """Build a recommendation for one event."""
        incident = await self._incident_from(event, kind)
        if incident is None:
            self.log.info(
                "decision.no_location",
                event=event.event_id,
                why="the event has no position and no zone, so distances cannot be computed",
            )
            return None

        responders = await self._responders()
        if not responders:
            self.log.info("decision.no_responders", event=event.event_id)

        options, solves = build_options(responders, [incident])
        rationale, degraded = (
            await llm_rationale(self.llm, options, [incident])
            if self.llm
            else (
                template_rationale(options, [incident]),
                "no model configured; using the template",
            )
        )
        decision = build_decision(
            tenant_id=event.tenant_id,
            options=options,
            solves=solves,
            incidents=[incident],
            responders=responders,
            rationale=rationale,
            degraded=degraded,
            trigger_event=event.event_id,
        )
        await self._persist(decision)
        self._recent.append(decision)
        del self._recent[: max(0, len(self._recent) - 50)]
        self._proposed += 1
        await self._emit(decision, ctx)
        self.log.info(
            "decision.proposed",
            decision=decision.decision_id,
            trigger=str(event.type),
            options=len(options),
            chosen=options[0].params.get("strategy") if options else None,
            awaiting_approval=True,
        )
        return decision

    async def _incident_from(self, event: Event, kind: str) -> Incident | None:
        """Locate the event, preferring its own position and falling back to its zone's centroid.

        An event with neither cannot be reasoned about spatially, and inventing a position would produce
        distances and ETAs that look real. Returning None is the honest outcome.
        """
        latitude = event.geo.lat if event.geo else None
        longitude = event.geo.lon if event.geo else None
        if latitude is None and event.zone_id:
            row = await self.pool.fetchrow(
                "SELECT ST_Y(ST_Centroid(geom::geometry)) AS lat, ST_X(ST_Centroid(geom::geometry)) AS lon "
                "FROM zones WHERE tenant_id = %s AND zone_id = %s",
                (event.tenant_id, event.zone_id),
            )
            if row and row.get("lat") is not None:
                latitude, longitude = float(row["lat"]), float(row["lon"])
        if latitude is None or longitude is None:
            return None
        return Incident(
            incident_id=event.event_id,
            kind=kind,
            lat=latitude,
            lon=longitude,
            severity=str(event.severity),
            zone_id=event.zone_id,
        )

    async def _responders(self) -> list[Responder]:
        """Who is available, from the world model.

        Read live rather than configured, because a responder that left the site an hour ago is not
        available and a static roster would happily dispatch it.
        """
        rows = await self.pool.fetch(
            """
            SELECT entity_id, type, label, payload,
                   ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon
              FROM entities
             WHERE tenant_id = %s AND NOT is_static AND geom IS NOT NULL
               AND (type = ANY(%s) OR payload->'attributes'->>%s = ANY(%s))
               AND last_seen >= now() - make_interval(secs => %s)
             ORDER BY last_seen DESC
             LIMIT 40
            """,
            (
                self.settings.tenant_id,
                list(RESPONDER_KINDS),
                RESPONDER_ROLE_ATTRIBUTE,
                sorted(RESPONDER_ROLES),
                self.settings.fusion_max_stale_s,
            ),
        )
        responders: list[Responder] = []
        for row in rows:
            payload = row.get("payload") or {}
            attributes = payload.get("attributes") or {}
            state = payload.get("state") or {}
            velocity = state.get("velocity") or {}
            speed = float(velocity.get("east", 0.0)) ** 2 + float(velocity.get("north", 0.0)) ** 2
            responders.append(
                Responder(
                    entity_id=str(row["entity_id"]),
                    # A marked role wins over the entity type: somebody asserting "this is a patrol" knows
                    # more than a classifier that said "person".
                    kind=(
                        "patrol"
                        if str(attributes.get(RESPONDER_ROLE_ATTRIBUTE, "")).lower()
                        in RESPONDER_ROLES
                        else RESPONDER_KINDS.get(str(row["type"]), "patrol")
                    ),
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                    # The entity's own typical speed, not its current one: a stationary drone is not a slow
                    # drone, and using the instantaneous value would make every parked responder look
                    # unreachable.
                    speed_mps=15.0 if row["type"] == "drone" else 6.0,
                    label=row.get("label"),
                    busy=speed > 1.0,
                    battery_pct=_battery_of(attributes),
                )
            )
        return responders

    async def _persist(self, decision: Decision) -> None:
        await self.pool.execute(
            """
            INSERT INTO decisions (
                tenant_id, decision_id, trigger_event, ts, chosen, rationale, confidence,
                approval, approved_by, approved_ts, executed_ts, solver, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, decision_id) DO UPDATE SET
                approval    = EXCLUDED.approval,
                approved_by = EXCLUDED.approved_by,
                approved_ts = EXCLUDED.approved_ts,
                executed_ts = EXCLUDED.executed_ts,
                chosen      = EXCLUDED.chosen,
                payload     = EXCLUDED.payload
            """,
            (
                decision.tenant_id,
                decision.decision_id,
                decision.trigger_event,
                decision.ts,
                decision.chosen,
                decision.rationale,
                decision.confidence,
                str(decision.approval),
                decision.approved_by,
                decision.approved_ts,
                decision.executed_ts,
                decision.solver,
                decision.to_json(),
            ),
        )

    async def _emit(self, decision: Decision, ctx: MessageContext | None) -> None:
        if ctx is not None:
            await ctx.publish(Topic.DECISIONS, decision)
        else:
            await self.publish(Topic.DECISIONS, decision)

    # -------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/decisions", tags=["decision"])
        async def decisions(
            approval: str | None = None, limit: int = Query(20, ge=1, le=200)
        ) -> dict[str, Any]:
            """Recent recommendations with their options and rationale."""
            rows = await self.pool.fetch(
                """
                SELECT payload FROM decisions
                 WHERE tenant_id = %s AND (%s::text IS NULL OR approval = %s)
                 ORDER BY ts DESC LIMIT %s
                """,
                (self.settings.tenant_id, approval, approval, limit),
            )
            return {"decisions": [row["payload"] for row in rows]}

        @app.get("/decisions/{decision_id}", tags=["decision"])
        async def decision_detail(decision_id: str) -> dict[str, Any]:
            row = await self.pool.fetchrow(
                "SELECT payload FROM decisions WHERE tenant_id = %s AND decision_id = %s",
                (self.settings.tenant_id, decision_id),
            )
            if row is None:
                raise HTTPException(status_code=404, detail=f"unknown decision {decision_id!r}")
            return dict(row["payload"])

        @app.post("/decisions/{decision_id}/approve", tags=["decision"])
        async def approve(decision_id: str, request: ApprovalRequest) -> dict[str, Any]:
            """Approve a recommendation. **The only path from proposal to action.**

            Approval records *which option* was chosen, because an operator may prefer the runner-up — and a
            gate that only accepted "yes" to the top option would make the ranked list decorative.
            """
            decision = await self._load(decision_id)
            if decision.approval != ApprovalState.PENDING:
                # Not an error to re-approve, but not a silent success either: saying what the state already
                # is beats pretending an action happened twice.
                raise HTTPException(
                    status_code=409,
                    detail=f"decision {decision_id} is already {decision.approval}",
                )

            chosen = request.option_id or decision.chosen
            if chosen and all(option.option_id != chosen for option in decision.options):
                raise HTTPException(
                    status_code=400,
                    detail=f"option {chosen!r} is not one of this decision's options",
                )

            decision.chosen = chosen
            decision.approval = ApprovalState.APPROVED
            decision.approved_by = request.approved_by
            decision.approved_ts = utc_now()
            if request.note:
                decision.explanation.notes.append(
                    f"approved by {request.approved_by}: {request.note}"
                )
            if request.option_id and request.option_id != decision.options[0].option_id:
                # Worth recording loudly: a human overriding the optimiser is the most interesting signal
                # this service produces, and it is how the objective gets improved.
                decision.explanation.notes.append(
                    f"the operator chose an option other than the recommendation "
                    f"({request.option_id}), which is a signal that the objective may be wrong"
                )
                self.log.info(
                    "decision.operator_override",
                    decision=decision_id,
                    chose=request.option_id,
                    recommended=decision.options[0].option_id,
                )

            await self._persist(decision)
            self._approved += 1
            await self._emit(decision, None)
            self.log.info(
                "decision.approved", decision=decision_id, by=request.approved_by, option=chosen
            )
            return {
                "decision_id": decision_id,
                "approval": str(decision.approval),
                "chosen": chosen,
                "approved_by": decision.approved_by,
                # Said explicitly, because the difference matters: approval authorises action, it does not
                # perform it. The agents service acts on approved decisions; this one never does.
                "note": "approved. This service does not execute; an approved decision is now actionable.",
            }

        @app.post("/decisions/{decision_id}/reject", tags=["decision"])
        async def reject(decision_id: str, request: RejectionRequest) -> dict[str, Any]:
            """Reject a recommendation, with the reason kept.

            The reason is the valuable part. A rejected recommendation with no reason teaches nobody
            anything; with one, it is evidence about where the objective is wrong.
            """
            decision = await self._load(decision_id)
            decision.approval = ApprovalState.REJECTED
            decision.approved_by = request.rejected_by
            decision.approved_ts = utc_now()
            decision.explanation.notes.append(
                f"rejected by {request.rejected_by}"
                + (f": {request.reason}" if request.reason else " (no reason given)")
            )
            await self._persist(decision)
            self._rejected += 1
            await self._emit(decision, None)
            self.log.info(
                "decision.rejected",
                decision=decision_id,
                by=request.rejected_by,
                reason=request.reason,
            )
            return {"decision_id": decision_id, "approval": str(decision.approval)}

        @app.post("/decisions/recommend", tags=["decision"])
        async def recommend_now(
            kind: str = "fire", zone_id: str = "dock_3", severity: str = "critical"
        ) -> dict[str, Any]:
            """Produce a recommendation on demand — the demo path, and a way to see the options list.

            The synthetic trigger is recorded as such, so a recommendation made by hand is never mistaken
            for one made in response to a detection.
            """
            event = Event(
                tenant_id=self.settings.tenant_id,
                type="fire_detected" if kind == "fire" else "unauthorized_entry",
                severity=Severity(severity),
                zone_id=zone_id,
                confidence=1.0,
                rule_id="decision.manual_request",
                attributes={"manual": True},
            )
            decision = await self.recommend_for(event, kind, None)
            if decision is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"could not locate zone {zone_id!r}; run: just seed",
                )
            return decision.to_wire()

        @app.post("/decisions/schedule/docks", tags=["decision"])
        async def schedule_docks(limit: int = Query(6, ge=1, le=20)) -> dict[str, Any]:
            """Schedule the trucks currently waiting onto the available docks."""
            from .solvers import DockRequest, solve_dock_schedule

            rows = await self.pool.fetch(
                """
                SELECT entity_id, label FROM entities
                 WHERE tenant_id = %s AND type = 'truck' AND NOT is_static
                   AND last_seen >= now() - make_interval(secs => %s)
                 ORDER BY first_seen ASC LIMIT %s
                """,
                (self.settings.tenant_id, self.settings.fusion_max_stale_s, limit),
            )
            docks = await self.pool.fetch(
                "SELECT zone_id FROM zones WHERE tenant_id = %s AND kind = 'dock' ORDER BY zone_id",
                (self.settings.tenant_id,),
            )
            requests = [
                DockRequest(
                    entity_id=str(row["entity_id"]),
                    duration_s=900,
                    earliest_s=index * 120,
                    priority=1,
                    label=row.get("label"),
                )
                for index, row in enumerate(rows)
            ]
            result = solve_dock_schedule(requests, [str(row["zone_id"]) for row in docks])
            return result.describe()

    async def _load(self, decision_id: str) -> Decision:
        row = await self.pool.fetchrow(
            "SELECT payload FROM decisions WHERE tenant_id = %s AND decision_id = %s",
            (self.settings.tenant_id, decision_id),
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown decision {decision_id!r}")
        return Decision.model_validate(row["payload"])


def _battery_of(attributes: dict[str, Any]) -> float | None:
    for key in ("battery_pct", "battery"):
        value = attributes.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


__all__ = ["ACTIONABLE", "DecisionService"]
