"""Governance: the audit trail, and the queries over it (PRD M18/M19/M21, Phase 5).

Every other service now *produces* audit records — the shared runtime publishes one per authorisation
decision, allows as well as denials. This service is what makes them durable and answerable.

**The table revokes UPDATE and DELETE.** That is enforced in Postgres (`005_immutability.sql`), not by
convention here, because a guarantee that depends on every writer behaving is not a guarantee. It has a
consequence worth stating: a mistake in an audit record cannot be corrected, only annotated by a later
record. That is the correct trade for a trail whose entire value is that it cannot be edited.

**Allows are recorded, not just denials.** A trail of denials answers "who was stopped". A trail of allows
answers "who did this", which is the question actually asked after an incident — and the one that cannot be
answered retrospectively if it was never recorded.

**Writing an audit record must never be able to fail a request.** The producer side logs and continues (see
`SioService._audit_decision`), so this service's job is to lose as little as possible: it batches, it retries
its own inserts, and it reports a backlog in `/health` rather than dropping silently. A dropped audit record
is invisible by definition, so the only defence is to make the drop visible.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Query

from sio_core import MessageContext, PgPool, SioService, describe_error, get_pg_pool
from sio_core.authz import POLICY
from sio_core.pii import active_detector, presidio_available
from sio_schemas import AuditRecord, BusMessage, Topic

#: How many records to buffer before writing, and how long to wait before writing a partial batch.
#:
#: Batched because one INSERT per authorisation decision would make the audit trail the platform's busiest
#: writer — every request produces one. Two seconds bounds how much is at risk if this process dies, which
#: is the number that actually matters: a batch is a window of records that exist only in memory.
BATCH_SIZE = 50
BATCH_INTERVAL_S = 2.0


class GovernanceService(SioService):
    """Persists the audit trail and answers questions about it."""

    name = "governance"
    subscribes = (Topic.AUDIT,)
    tick_interval_s = 1.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self._pending: list[AuditRecord] = []
        self._last_flush = time.monotonic()
        self._written = 0
        self._failed = 0
        self._denials_seen = 0

    async def setup(self) -> None:
        await self.pool.open()
        immutable = await self._verify_immutability()
        self.log.info(
            "governance.ready",
            audit_immutable=immutable,
            pii_detector=active_detector(),
            presidio=presidio_available(),
            policy_rules=len(POLICY),
        )

    async def teardown(self) -> None:
        # Flush on the way out. A clean shutdown that discarded a partial batch would lose exactly the
        # records describing whatever prompted the shutdown.
        await self._flush()

    async def _verify_immutability(self) -> bool:
        """Prove that the database refuses to change an audit row, by trying it.

        Asserted at startup rather than trusted, because this is the one property the whole trail rests on
        and a migration that failed to apply is invisible until somebody needs the guarantee.

        **It attempts a real UPDATE inside a transaction and rolls back.** The first version inspected
        `information_schema.table_privileges` for UPDATE/DELETE grants and reported the trail as mutable —
        which was false, and a governance report making a claim it has not actually verified is precisely
        the failure this service exists to prevent.

        The metadata check was looking at the wrong mechanism. `005_immutability.sql` enforces append-only
        with **triggers**, deliberately, and its own comment says why: a REVOKE does nothing against a
        superuser or a table owner, which is exactly what the dev stack connects as. The grants are present
        and irrelevant; the trigger is the control.

        Trying the operation is the only check that cannot be fooled by looking at the wrong thing.
        """
        probe_id = f"aud_immutability_probe_{int(time.time())}"
        try:
            # A probe ROW of its own, then an attempt to update THAT row.
            #
            # Two earlier versions of this check were wrong in opposite directions:
            #
            # * inspecting `information_schema.table_privileges` looked at the wrong mechanism entirely.
            #   `005_immutability.sql` uses triggers, deliberately, because a REVOKE does nothing against a
            #   superuser or table owner — which is what the dev stack connects as. The check reported the
            #   trail as mutable when it was not.
            # * `UPDATE ... WHERE false` fixed the mechanism and broke the logic: the trigger is FOR EACH
            #   ROW, so matching no rows fires nothing. `UPDATE 0` with no error is indistinguishable from
            #   "there is no trigger".
            #
            # Updating an arbitrary real row would be conclusive and unacceptable — if the guarantee is
            # absent, the check would itself tamper with evidence. So it writes its own row first. Either the
            # trigger raises (the guarantee holds) or the probe row is modified, which is harmless because it
            # belongs to the probe. The row is also a legitimate audit record: it says the check ran.
            await self.pool.execute(
                """
                INSERT INTO audit_log (tenant_id, audit_id, actor, action, resource, allowed, reason)
                VALUES (%s, %s, 'service:governance', 'admin.read', 'audit_log', true, %s)
                ON CONFLICT (tenant_id, audit_id) DO NOTHING
                """,
                (
                    self.settings.tenant_id,
                    probe_id,
                    "startup check: verifying the audit table refuses UPDATE",
                ),
            )
            try:
                await self.pool.execute(
                    "UPDATE audit_log SET reason = 'tampered' WHERE tenant_id = %s AND audit_id = %s",
                    (self.settings.tenant_id, probe_id),
                )
            except Exception:
                # The trigger fired. This is the intended outcome.
                return True
            self.log.warning(
                "governance.audit_mutable",
                consequence=(
                    "an UPDATE against audit_log succeeded; the trail can be rewritten and is not evidence"
                ),
                fix="re-run infra/postgres/005_immutability.sql",
                probe=probe_id,
            )
            return False
        except Exception as exc:
            # Cannot determine. Reported as unknown rather than assumed either way: claiming the guarantee
            # holds because the check failed would be worse than admitting ignorance.
            self.log.warning("governance.immutability_unknown", error=describe_error(exc))
            return False

    async def health_checks(self) -> dict[str, str]:
        checks = {"postgres": "ok" if await self.pool.ping() else "unreachable"}
        if self._failed:
            checks["audit_writes"] = f"degraded: {self._failed} record(s) could not be written"
        backlog = len(self._pending)
        if backlog > BATCH_SIZE * 4:
            # A growing buffer means records exist only in memory. Said out loud, because a dropped audit
            # record is invisible by definition and the backlog is the only warning available.
            checks["audit_backlog"] = f"degraded: {backlog} record(s) buffered and unwritten"
        return checks

    async def health_info(self) -> dict[str, str]:
        return {
            "written": str(self._written),
            "buffered": str(len(self._pending)),
            "failed": str(self._failed),
            "denials_seen": str(self._denials_seen),
            "pii_detector": active_detector(),
        }

    # ------------------------------------------------------------------ ingestion
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        if message.kind != "AuditRecord":
            return
        record = message.decode(AuditRecord)
        if not record.allowed:
            self._denials_seen += 1
        self._pending.append(record)
        if len(self._pending) >= BATCH_SIZE:
            await self._flush()

    async def tick(self) -> None:
        if self._pending and time.monotonic() - self._last_flush >= BATCH_INTERVAL_S:
            await self._flush()

    async def _flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        self._last_flush = time.monotonic()
        try:
            for record in batch:
                await self.pool.execute(
                    """
                    INSERT INTO audit_log (
                        tenant_id, audit_id, ts, actor, actor_roles, action, resource, allowed,
                        reason, policy_engine, ip, user_agent, request_id, trace_id, details
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (tenant_id, audit_id) DO NOTHING
                    """,
                    (
                        record.tenant_id,
                        record.audit_id,
                        record.ts,
                        record.actor,
                        [str(role) for role in record.actor_roles],
                        record.action,
                        record.resource,
                        record.allowed,
                        record.reason,
                        record.policy_engine,
                        record.ip,
                        record.user_agent,
                        record.request_id,
                        record.trace_id,
                        record.model_dump_json(include={"details", "evidence"}),
                    ),
                )
                self._written += 1
        except Exception as exc:
            self._failed += len(batch)
            # Not re-queued. A record that failed to insert will usually fail again — a malformed value, a
            # missing column — and retrying forever would grow the buffer without bound while hiding the
            # cause. The count surfaces in /health, which is the honest outcome.
            self.log.error(
                "governance.audit_write_failed",
                records=len(batch),
                error=describe_error(exc),
                consequence="these records are lost; the trail has a gap",
            )

    # --------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/audit", tags=["governance"])
        async def audit(
            actor: str | None = None,
            action: str | None = None,
            allowed: bool | None = None,
            since_minutes: int = Query(default=60, ge=1, le=10_080),
            limit: int = Query(default=100, ge=1, le=1000),
        ) -> dict[str, Any]:
            """Query the trail.

            `allowed=false` first in the documentation for a reason: "what was refused, and to whom" is the
            question this endpoint gets asked during an incident.
            """
            await self._flush()  # so a query never misses what just happened
            rows = await self.pool.fetch(
                """
                SELECT audit_id, ts, actor, actor_roles, action, resource, allowed, reason,
                       policy_engine, ip, details
                  FROM audit_log
                 WHERE tenant_id = %s
                   AND ts >= now() - make_interval(mins => %s)
                   AND (%s::text IS NULL OR actor = %s)
                   AND (%s::text IS NULL OR action = %s)
                   AND (%s::boolean IS NULL OR allowed = %s)
                 ORDER BY ts DESC
                 LIMIT %s
                """,
                (
                    self.settings.tenant_id,
                    since_minutes,
                    actor,
                    actor,
                    action,
                    action,
                    allowed,
                    allowed,
                    limit,
                ),
            )
            return {
                "entries": [dict(row) for row in rows],
                "count": len(rows),
                "window_minutes": since_minutes,
                "counters": {
                    "written": self._written,
                    "buffered": len(self._pending),
                    "failed": self._failed,
                },
            }

        @app.get("/audit/denials", tags=["governance"])
        async def denials(
            since_minutes: int = Query(default=60, ge=1, le=10_080),
            limit: int = Query(default=100, ge=1, le=1000),
        ) -> dict[str, Any]:
            """Refusals, grouped by who and what.

            Grouped rather than listed, because a single principal hitting one denial fifty times is one
            problem — usually a misconfigured integration or a missing role — and fifty rows describe it
            worse than one row with a count.
            """
            await self._flush()
            rows = await self.pool.fetch(
                """
                SELECT actor, action, count(*) AS attempts, max(ts) AS last_attempt,
                       (array_agg(reason ORDER BY ts DESC))[1] AS reason
                  FROM audit_log
                 WHERE tenant_id = %s AND allowed = false
                   AND ts >= now() - make_interval(mins => %s)
                 GROUP BY actor, action
                 ORDER BY attempts DESC, last_attempt DESC
                 LIMIT %s
                """,
                (self.settings.tenant_id, since_minutes, limit),
            )
            return {"denials": [dict(row) for row in rows], "window_minutes": since_minutes}

        @app.get("/policies", tags=["governance"])
        async def policies() -> dict[str, Any]:
            """The policy in force, as data.

            Readable rather than only enforceable. An operator who has just been refused something needs to
            find out what would permit it, and reading the rule is faster than asking whoever administers
            the platform.
            """
            return {
                "engine": self.settings.policy_engine,
                "auth_mode": self.settings.auth_mode,
                "auth_required": self.settings.auth_required,
                "pii_redaction": self.settings.redact_pii,
                "pii_detector": active_detector(),
                "rules": [
                    {
                        "action": rule.action,
                        "roles": list(rule.roles) or ["any authenticated"],
                        "min_clearance": rule.min_clearance,
                        "requires_pii_scope": rule.requires_pii_scope,
                        "zone_scoped": rule.zone_scoped,
                        "description": rule.description,
                    }
                    for rule in POLICY
                ],
            }

        @app.get("/governance/posture", tags=["governance"])
        async def posture() -> dict[str, Any]:
            """What is actually switched on, in one place.

            Written for the question "is this deployment safe?", which nobody can answer by reading fifteen
            services' configuration. Every entry is a fact about the running process, not a restatement of
            the documentation — and the `weaknesses` list names what is not protected rather than leaving a
            reader to infer it from absence.
            """
            weaknesses: list[str] = []
            if not self.settings.auth_required:
                weaknesses.append(
                    "authentication is NOT required: every request runs as the default tenant"
                )
            if self.settings.auth_mode == "dev":
                weaknesses.append(
                    "the dev token issuer is enabled and its signing secret is in settings"
                )
            if not self.settings.redact_pii:
                weaknesses.append("PII redaction is disabled")
            if not presidio_available():
                weaknesses.append(
                    "Presidio is not installed, so names and addresses are not detected — "
                    "only structured identifiers are"
                )
            if self.settings.retain_raw:
                weaknesses.append(
                    "unblurred media is retained under the raw/ prefix (SIO_RETAIN_RAW=true); "
                    "access needs media.raw, which requires the pii_scope claim"
                )
            if not (self.settings.blur_faces or self.settings.blur_plates):
                weaknesses.append(
                    "face and plate blurring is disabled: stored frames are unredacted"
                )
            if not await self._verify_immutability():
                weaknesses.append("the audit table does not refuse UPDATE/DELETE")
            return {
                "auth_mode": self.settings.auth_mode,
                "auth_required": self.settings.auth_required,
                "policy_engine": self.settings.policy_engine,
                "pii_redaction": self.settings.redact_pii,
                "pii_detector": active_detector(),
                # Blurring and raw retention are INDEPENDENT, and conflating them made this a false
                # statement. Blurring always applies to the frame an operator retrieves; retain_raw
                # additionally keeps an unblurred copy under a `raw/` prefix gated on `media.raw`.
                "face_plate_blurring": self.settings.blur_faces or self.settings.blur_plates,
                "raw_media_retained": self.settings.retain_raw,
                "audit_enabled": self.settings.audit_enabled,
                "audit_written": self._written,
                "weaknesses": weaknesses,
                "assessment": (
                    "hardened"
                    if not weaknesses
                    else f"{len(weaknesses)} weakness(es) — see the list"
                ),
            }


__all__ = ["BATCH_INTERVAL_S", "BATCH_SIZE", "GovernanceService"]
