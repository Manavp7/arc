"""Alerts service: prioritise, fold, escalate, and let a human close the loop (PRD M16)."""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from sio_core import MessageContext, PgPool, SioService, describe_error, get_pg_pool
from sio_core.explain import ExplanationBuilder
from sio_schemas import (
    Alert,
    AlertState,
    BusMessage,
    Event,
    Severity,
    Topic,
    utc_now,
)

from .scoring import (
    DEDUP_WINDOW_S,
    group_key,
    score_alert,
    should_escalate,
    title_for,
    within_dedup_window,
)

#: Severities worth an alert. Everything below is timeline material.
#:
#: An inbox that contains every zone entry is an inbox nobody opens, and then the criticals in it are
#: invisible too. Raising the floor is the single most effective thing that can be done for an alerting
#: system's usefulness.
ALERTABLE = (Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)

#: Consecutive webhook failures before the circuit opens, and how long it stays open.
#:
#: Measured the hard way: a misconfigured URL produced one warning per alert, and on a backlog of a few
#: thousand alerts the log became unreadable. An endpoint that has refused five times in a row is down or
#: wrong, and hammering it neither helps it nor informs us.
WEBHOOK_FAILURES_BEFORE_PAUSE = 5
WEBHOOK_PAUSE_S = 60.0


class AckRequest(BaseModel):
    ack_by: str = "operator"
    note: str | None = None
    assignee: str | None = None


class ResolveRequest(BaseModel):
    resolved_by: str = "operator"
    note: str | None = None


class AlertsService(SioService):
    """Turns events into a prioritised, deduplicated inbox."""

    name = "alerts"
    subscribes = (Topic.EVENTS, Topic.DECISIONS)
    tick_interval_s = 30.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self.client = httpx.AsyncClient(timeout=8.0)
        self._raised = 0
        self._folded = 0
        self._escalated = 0
        self._acked = 0
        self._resolved = 0
        self._webhooks_sent = 0
        self._webhooks_failed = 0
        self._webhook_consecutive_failures = 0
        self._webhook_open_until = 0.0

    async def setup(self) -> None:
        await self.pool.open()
        self.log.info(
            "alerts.ready",
            alertable=[str(severity) for severity in ALERTABLE],
            dedup_window_s=DEDUP_WINDOW_S,
            webhook=bool(self.settings.alert_webhook_url),
        )

    async def teardown(self) -> None:
        await self.client.aclose()

    async def health_checks(self) -> dict[str, str]:
        checks = {"postgres": "ok" if await self.pool.ping() else "unreachable"}
        if self._webhooks_failed:
            paused = time.monotonic() < self._webhook_open_until
            checks["webhook"] = f"degraded: {self._webhooks_failed} delivery failure(s)" + (
                f", paused for {self._webhook_open_until - time.monotonic():.0f}s" if paused else ""
            )
        return checks

    async def health_info(self) -> dict[str, str]:
        row = await self.pool.fetchrow(
            "SELECT count(*) FILTER (WHERE state = 'open') AS open, "
            "       count(*) FILTER (WHERE state = 'escalated') AS escalated "
            "  FROM alerts WHERE tenant_id = %s",
            (self.settings.tenant_id,),
        )
        return {
            "raised": str(self._raised),
            "folded_into_existing": str(self._folded),
            "escalated": str(self._escalated),
            "acknowledged": str(self._acked),
            "resolved": str(self._resolved),
            "open_now": str((row or {}).get("open") or 0),
            "escalated_now": str((row or {}).get("escalated") or 0),
            "webhooks_sent": str(self._webhooks_sent),
        }

    # ------------------------------------------------------------------ handling
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        if message.kind == "Event":
            await self._on_event(message.decode(Event), ctx)
        elif message.kind == "Decision":
            await self._link_decision(message)

    async def _on_event(self, event: Event, ctx: MessageContext | None) -> None:
        if event.severity not in ALERTABLE:
            return
        if event.rule_id and event.rule_id.startswith("workflow."):
            # Playbook progress is not an alert. It is the *response* to one, and putting it in the inbox
            # would double every incident: one row for the fire, five for the response to it.
            return

        key = group_key(event)
        existing = await self._open_alert_for(key)
        if existing is not None and within_dedup_window(existing.last_ts):
            await self._fold(existing, event, ctx)
            return
        await self._raise(event, key, ctx)

    async def _raise(self, event: Event, key: str, ctx: MessageContext | None) -> Alert:
        scored = score_alert(
            severity=event.severity,
            confidence=event.confidence,
            zone_id=event.zone_id,
            last_ts=event.ts,
        )
        explanation = ExplanationBuilder(summary=title_for(event))
        explanation.add_event(event)
        explanation.add_note(f"priority {scored.score:.1f}: {scored.reason}")
        for name, value in scored.factors.items():
            explanation.add_note(f"factor {name}: {value:g}")
        # The originating explanation is carried through rather than replaced. The events engine knew why it
        # fired; an alert that discards that reasoning makes the operator go and find it.
        for note in event.explanation.notes[:6]:
            explanation.add_note(f"from the event: {note}")
        explanation.confidence(event.confidence)

        alert = Alert(
            tenant_id=event.tenant_id,
            title=title_for(event),
            event_ids=[event.event_id],
            entity_ids=list(event.entities),
            severity=event.severity,
            score=scored.score,
            group_key=key,
            state=AlertState.OPEN,
            ts=event.ts,
            last_ts=event.ts,
            geo=event.geo,
            zone_id=event.zone_id,
            explanation=explanation.build(),
            urgency_reason=scored.reason,
        )
        await self._persist(alert)
        self._raised += 1
        await self._emit(alert, ctx)
        await self._webhook(alert, "raised")
        self.log.info(
            "alerts.raised",
            alert=alert.alert_id,
            severity=str(alert.severity),
            score=alert.score,
            group=key,
        )
        return alert

    async def _fold(self, alert: Alert, event: Event, ctx: MessageContext | None) -> None:
        """Fold a repeat into an existing alert rather than raising another.

        The count and the score rise; the *original* timestamp does not. An alert that keeps resetting its
        own age can never escalate — it would look permanently fresh while nobody attends to it, which is
        exactly the failure escalation exists to catch.
        """
        alert.count += 1
        alert.last_ts = max(alert.last_ts, event.ts)
        if event.event_id not in alert.event_ids:
            alert.event_ids = [*alert.event_ids[-19:], event.event_id]
        for entity in event.entities:
            if entity not in alert.entity_ids:
                alert.entity_ids.append(entity)
        if event.severity != alert.severity and _rank(event.severity) > _rank(alert.severity):
            # A repeat can be worse than the original. Taking the maximum means an incident that escalates
            # in reality escalates in the inbox.
            alert.severity = event.severity

        scored = score_alert(
            severity=alert.severity,
            confidence=event.confidence,
            zone_id=alert.zone_id,
            last_ts=alert.last_ts,
            count=alert.count,
        )
        alert.score = scored.score
        alert.urgency_reason = scored.reason
        alert.explanation.notes.append(
            f"folded in {event.event_id} ({alert.count} occurrences); priority now {scored.score:.1f}"
        )
        await self._persist(alert)
        self._folded += 1
        await self._emit(alert, ctx)

    async def _link_decision(self, message: BusMessage) -> None:
        """Attach a decision to the alert it responds to, so the inbox shows what is being done."""
        from sio_schemas import Decision

        decision = message.decode(Decision)
        if not decision.trigger_event:
            return
        row = await self.pool.fetchrow(
            "SELECT payload FROM alerts WHERE tenant_id = %s AND %s = ANY(event_ids) "
            "ORDER BY last_ts DESC LIMIT 1",
            (self.settings.tenant_id, decision.trigger_event),
        )
        if row is None:
            return
        alert = Alert.model_validate(row["payload"])
        if decision.decision_id in alert.decision_ids:
            return
        alert.decision_ids.append(decision.decision_id)
        alert.explanation.notes.append(
            f"a recommendation was produced ({decision.decision_id}), awaiting {decision.approval}"
        )
        await self._persist(alert)

    # ---------------------------------------------------------------------- tick
    async def tick(self) -> None:
        """Escalate what has been waiting too long."""
        rows = await self.pool.fetch(
            "SELECT payload FROM alerts WHERE tenant_id = %s AND state = 'open' "
            "ORDER BY score DESC LIMIT 200",
            (self.settings.tenant_id,),
        )
        now = utc_now()
        for row in rows:
            alert = Alert.model_validate(row["payload"])
            escalate, reason = should_escalate(
                severity=alert.severity,
                state=alert.state,
                ts=alert.ts,
                ack_ts=alert.ack_ts,
                now=now,
            )
            if not escalate:
                # Rescore anyway, so the inbox order reflects ageing rather than only arrival.
                fresh = score_alert(
                    severity=alert.severity,
                    confidence=alert.explanation.confidence or 0.9,
                    zone_id=alert.zone_id,
                    last_ts=alert.last_ts,
                    count=alert.count,
                    now=now,
                )
                if abs(fresh.score - alert.score) > 0.05:
                    alert.score = fresh.score
                    alert.urgency_reason = fresh.reason
                    await self._persist(alert)
                continue

            alert.state = AlertState.ESCALATED
            alert.escalated_ts = now
            alert.explanation.notes.append(f"escalated: {reason}")
            # The scoring reason is NOT overwritten. It answers "why is this here", which escalation does
            # not change — and overwriting it produced an inbox where every row's justification for its
            # priority was the escalation timer, still reading "unacknowledged" after being acknowledged.
            alert.escalation_reason = reason
            await self._persist(alert)
            self._escalated += 1
            await self._emit(alert, None)
            await self._webhook(alert, "escalated")
            self.log.warning(
                "alerts.escalated",
                alert=alert.alert_id,
                severity=str(alert.severity),
                reason=reason,
            )

    # ---------------------------------------------------------------- persistence
    async def _open_alert_for(self, key: str) -> Alert | None:
        row = await self.pool.fetchrow(
            "SELECT payload FROM alerts WHERE tenant_id = %s AND group_key = %s "
            "  AND state IN ('open', 'escalated') ORDER BY last_ts DESC LIMIT 1",
            (self.settings.tenant_id, key),
        )
        return Alert.model_validate(row["payload"]) if row else None

    async def _persist(self, alert: Alert) -> None:
        await self.pool.execute(
            """
            INSERT INTO alerts (
                tenant_id, alert_id, title, group_key, severity, score, state, count, ts, last_ts,
                geom, zone_id, event_ids, entity_ids, decision_ids, ack_by, ack_ts, escalated_ts,
                resolved_ts, assignee, urgency_reason, escalation_reason, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s::jsonb)
            ON CONFLICT (tenant_id, alert_id) DO UPDATE SET
                severity       = EXCLUDED.severity,
                score          = EXCLUDED.score,
                state          = EXCLUDED.state,
                count          = EXCLUDED.count,
                last_ts        = EXCLUDED.last_ts,
                event_ids      = EXCLUDED.event_ids,
                entity_ids     = EXCLUDED.entity_ids,
                decision_ids   = EXCLUDED.decision_ids,
                ack_by         = EXCLUDED.ack_by,
                ack_ts         = EXCLUDED.ack_ts,
                escalated_ts   = EXCLUDED.escalated_ts,
                resolved_ts    = EXCLUDED.resolved_ts,
                assignee       = EXCLUDED.assignee,
                urgency_reason = EXCLUDED.urgency_reason,
                escalation_reason = EXCLUDED.escalation_reason,
                payload        = EXCLUDED.payload
            """,
            (
                alert.tenant_id,
                alert.alert_id,
                alert.title,
                alert.group_key,
                str(alert.severity),
                alert.score,
                str(alert.state),
                alert.count,
                alert.ts,
                alert.last_ts,
                f"SRID=4326;POINT({alert.geo.lon} {alert.geo.lat})" if alert.geo else None,
                alert.zone_id,
                alert.event_ids,
                alert.entity_ids,
                alert.decision_ids,
                alert.ack_by,
                alert.ack_ts,
                alert.escalated_ts,
                alert.resolved_ts,
                alert.assignee,
                alert.urgency_reason,
                alert.escalation_reason,
                alert.to_json(),
            ),
        )

    async def _emit(self, alert: Alert, ctx: MessageContext | None) -> None:
        if ctx is not None:
            await ctx.publish(Topic.ALERTS, alert)
        else:
            await self.publish(Topic.ALERTS, alert)

    async def _webhook(self, alert: Alert, action: str) -> None:
        """Fan out to an external endpoint, if one is configured.

        Failures are counted and surfaced in `/health`, never retried in-line. A webhook that blocks alert
        processing turns somebody else's outage into ours, and the alert is already durable in Postgres — the
        webhook is a convenience, not the record.
        """
        url = self.settings.alert_webhook_url
        if not url:
            return
        if time.monotonic() < self._webhook_open_until:
            # Circuit open. A dead endpoint used to produce one warning per alert, and during a busy
            # minute that buries everything else in the log — the noise costs more than the webhook is
            # worth. Health still reports the failures, so this is quieter, not hidden.
            return
        try:
            response = await self.client.post(
                url,
                json={
                    "action": action,
                    "alert_id": alert.alert_id,
                    "title": alert.title,
                    "severity": str(alert.severity),
                    "score": alert.score,
                    "zone_id": alert.zone_id,
                    "count": alert.count,
                    "urgency_reason": alert.urgency_reason,
                    "ts": alert.last_ts.isoformat(),
                },
            )
            response.raise_for_status()
            self._webhooks_sent += 1
            self._webhook_consecutive_failures = 0
        except httpx.HTTPError as exc:
            self._webhooks_failed += 1
            self._webhook_consecutive_failures += 1
            if self._webhook_consecutive_failures >= WEBHOOK_FAILURES_BEFORE_PAUSE:
                self._webhook_open_until = time.monotonic() + WEBHOOK_PAUSE_S
                self.log.warning(
                    "alerts.webhook_paused",
                    url=url,
                    failures=self._webhook_consecutive_failures,
                    resuming_in_s=WEBHOOK_PAUSE_S,
                    error=describe_error(exc),
                )
            else:
                self.log.warning("alerts.webhook_failed", url=url, error=describe_error(exc))

    # -------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/alerts", tags=["alerts"])
        async def alerts(
            state: str | None = None,
            grouped: bool = True,
            limit: int = Query(50, ge=1, le=500),
        ) -> dict[str, Any]:
            """The inbox, highest priority first.

            Grouped by default: an operator wants "three fires, one intrusion", not sixty rows. The count is
            on the group, and the individual events are reachable from the alert.
            """
            rows = await self.pool.fetch(
                """
                SELECT payload FROM alerts
                 WHERE tenant_id = %s AND (%s::text IS NULL OR state = %s)
                 ORDER BY
                   CASE state WHEN 'escalated' THEN 0 WHEN 'open' THEN 1 ELSE 2 END,
                   score DESC, last_ts DESC
                 LIMIT %s
                """,
                (self.settings.tenant_id, state, state, limit),
            )
            items = [row["payload"] for row in rows]
            if not grouped:
                return {"alerts": items}
            groups: dict[str, dict[str, Any]] = {}
            for item in items:
                bucket = str(item.get("group_key", "")).split(":")[0]
                group = groups.setdefault(
                    bucket, {"kind": bucket, "count": 0, "max_score": 0.0, "alerts": []}
                )
                group["count"] += int(item.get("count", 1))
                group["max_score"] = max(group["max_score"], float(item.get("score", 0)))
                group["alerts"].append(item)
            return {
                "alerts": items,
                "groups": sorted(groups.values(), key=lambda group: -group["max_score"]),
                "open": sum(1 for item in items if item.get("state") == "open"),
                "escalated": sum(1 for item in items if item.get("state") == "escalated"),
            }

        @app.get("/alerts/{alert_id}", tags=["alerts"])
        async def alert_detail(alert_id: str) -> dict[str, Any]:
            return (await self._load(alert_id)).to_wire()

        @app.post("/alerts/{alert_id}/ack", tags=["alerts"])
        async def acknowledge(alert_id: str, request: AckRequest) -> dict[str, Any]:
            """Acknowledge an alert: somebody has seen it and owns it.

            Acknowledging stops the escalation timer, which is the whole point — escalation is about whether
            a human is engaged, not about the event getting worse.
            """
            alert = await self._load(alert_id)
            if alert.state == AlertState.RESOLVED:
                raise HTTPException(status_code=409, detail="this alert is already resolved")
            alert.state = AlertState.ACKNOWLEDGED
            alert.ack_by = request.ack_by
            alert.ack_ts = utc_now()
            alert.assignee = request.assignee or request.ack_by
            # No longer unacknowledged, so the sentence saying it is must go. A stale reason is worse than
            # none: it contradicts the state shown beside it.
            alert.escalation_reason = None
            alert.explanation.notes.append(
                f"acknowledged by {request.ack_by}"
                + (f": {request.note}" if request.note else "")
                + f" after {(alert.ack_ts - alert.ts).total_seconds() / 60:.1f} min"
            )
            await self._persist(alert)
            self._acked += 1
            await self._emit(alert, None)
            self.log.info("alerts.acknowledged", alert=alert_id, by=request.ack_by)
            return alert.to_wire()

        @app.post("/alerts/{alert_id}/resolve", tags=["alerts"])
        async def resolve(alert_id: str, request: ResolveRequest) -> dict[str, Any]:
            alert = await self._load(alert_id)
            alert.state = AlertState.RESOLVED
            alert.resolved_ts = utc_now()
            alert.explanation.notes.append(
                f"resolved by {request.resolved_by}"
                + (f": {request.note}" if request.note else " (no note)")
                + f" after {(alert.resolved_ts - alert.ts).total_seconds() / 60:.1f} min"
            )
            await self._persist(alert)
            self._resolved += 1
            await self._emit(alert, None)
            return alert.to_wire()

        @app.post("/alerts/{alert_id}/escalate", tags=["alerts"])
        async def escalate_now(alert_id: str, reason: str = "escalated by hand") -> dict[str, Any]:
            alert = await self._load(alert_id)
            alert.state = AlertState.ESCALATED
            alert.escalated_ts = utc_now()
            alert.escalation_reason = reason
            alert.explanation.notes.append(f"escalated by hand: {reason}")
            await self._persist(alert)
            self._escalated += 1
            await self._emit(alert, None)
            await self._webhook(alert, "escalated")
            return alert.to_wire()

        @app.get("/alerts/stats/summary", tags=["alerts"])
        async def summary() -> dict[str, Any]:
            """Inbox health: how much is open, how old, and how long acknowledgement takes.

            The last figure is the one that matters. A queue of open alerts is normal; a *rising* time to
            acknowledge means the inbox is being ignored, and that is the failure that makes alerting
            worthless.
            """
            rows = await self.pool.fetch(
                """
                SELECT state, count(*) AS n,
                       avg(extract(epoch from (coalesce(ack_ts, now()) - ts))) AS mean_age_s,
                       max(score) AS top_score
                  FROM alerts WHERE tenant_id = %s GROUP BY state
                """,
                (self.settings.tenant_id,),
            )
            acked = await self.pool.fetchrow(
                "SELECT avg(extract(epoch from (ack_ts - ts))) AS mean_ack_s, count(*) AS n "
                "  FROM alerts WHERE tenant_id = %s AND ack_ts IS NOT NULL",
                (self.settings.tenant_id,),
            )
            return {
                "by_state": {
                    str(row["state"]): {
                        "count": int(row["n"]),
                        "mean_age_s": round(float(row["mean_age_s"] or 0), 1),
                        "top_score": round(float(row["top_score"] or 0), 2),
                    }
                    for row in rows
                },
                "mean_time_to_acknowledge_s": round(float((acked or {}).get("mean_ack_s") or 0), 1),
                "acknowledged_count": int((acked or {}).get("n") or 0),
                "counters": {
                    "raised": self._raised,
                    "folded": self._folded,
                    "escalated": self._escalated,
                    "webhooks_sent": self._webhooks_sent,
                    "webhooks_failed": self._webhooks_failed,
                },
            }

    async def _load(self, alert_id: str) -> Alert:
        row = await self.pool.fetchrow(
            "SELECT payload FROM alerts WHERE tenant_id = %s AND alert_id = %s",
            (self.settings.tenant_id, alert_id),
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown alert {alert_id!r}")
        return Alert.model_validate(row["payload"])


def _rank(severity: Any) -> int:
    return {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}.get(str(severity), 2)


__all__ = ["ALERTABLE", "AlertsService"]
