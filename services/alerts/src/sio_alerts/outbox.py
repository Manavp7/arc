"""Postgres outbox for alert notifications; HTTP always runs outside the transaction.

Leases prevent concurrent normal dispatch. After a crash an unconfirmed request can
be sent again, so receivers must deduplicate the stable Idempotency-Key. A 2xx that
has been committed is terminal and cannot be manually retried.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from fastapi import HTTPException

from sio_core import PgPool
from sio_schemas import Alert

MAX_ATTEMPTS = 5
LEASE_S = 60
REQUEST_TIMEOUT_S = 10
CONCURRENCY = 4


def destination_label(url: str) -> str | None:
    """Never expose userinfo, paths or query tokens in delivery history."""
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "Configured endpoint"
        return f"{parsed.scheme}://{parsed.hostname}"
    except ValueError:
        return "Configured endpoint"


def delivery_id(alert: Alert, action: str) -> str:
    stamp = alert.escalated_ts if action == "escalated" else alert.ts
    return (
        "adl_"
        + uuid5(
            NAMESPACE_URL, json.dumps([alert.tenant_id, alert.alert_id, action, str(stamp)])
        ).hex
    )


def retry_delay(attempt: int) -> int:
    return min(300, 5 * 2 ** min(max(attempt - 1, 0), 6))


def enqueue_with_alert(
    statement: str, params: tuple[Any, ...], alert: Alert, action: str, destination: str
) -> tuple[str, tuple[Any, ...]]:
    """A single SQL statement commits both the alert mutation and its notification."""
    payload = {
        "delivery_id": delivery_id(alert, action),
        "action": action,
        "alert_id": alert.alert_id,
        "title": alert.title,
        "severity": str(alert.severity),
        "score": alert.score,
        "zone_id": alert.zone_id,
        "count": alert.count,
        "urgency_reason": alert.urgency_reason,
        "ts": alert.last_ts.isoformat(),
    }
    sql = f"""
        WITH alert_write AS ({statement} RETURNING tenant_id, alert_id), queued AS (
            INSERT INTO alert_deliveries
                (tenant_id, delivery_id, alert_id, action, title, destination_url, payload)
            SELECT tenant_id, %s, alert_id, %s, %s, %s, %s::jsonb FROM alert_write
            ON CONFLICT (tenant_id, delivery_id) DO NOTHING
            RETURNING tenant_id, delivery_id
        )
        INSERT INTO alert_delivery_history (tenant_id, delivery_id, kind)
        SELECT tenant_id, delivery_id, 'queued' FROM queued
    """
    return sql, (
        *params,
        payload["delivery_id"],
        action,
        alert.title,
        destination,
        json.dumps(payload),
    )


class AlertOutbox:
    def __init__(self, pool: PgPool, client: httpx.AsyncClient) -> None:
        self.pool = pool
        self.client = client

    async def recover(self, tenant: str, destination: str) -> None:
        # Small batches prevent an old backlog monopolising alert escalation.
        await self.pool.execute(
            """
            WITH candidates AS (
                SELECT tenant_id, delivery_id FROM alert_deliveries
                WHERE tenant_id = %s AND (
                    (status = 'sending' AND lease_until <= now()) OR
                    (status = 'pending' AND destination_url <> %s))
                ORDER BY created_at LIMIT 100 FOR UPDATE SKIP LOCKED
            ), changed AS (
                UPDATE alert_deliveries d SET
                    status = CASE WHEN destination_url <> %s THEN 'blocked'
                        WHEN cycle_attempts >= %s THEN 'failed' ELSE 'pending' END,
                    error = CASE WHEN destination_url <> %s THEN
                        'Delivery paused: the configured endpoint changed or was disabled.'
                        ELSE 'Previous delivery was interrupted; receiver acceptance is unknown.' END,
                    next_attempt_at = CASE WHEN destination_url = %s AND cycle_attempts < %s
                        THEN now() ELSE NULL END,
                    lease_token = NULL, lease_until = NULL, updated_at = now()
                FROM candidates c WHERE d.tenant_id = c.tenant_id AND d.delivery_id = c.delivery_id
                RETURNING d.*
            ) INSERT INTO alert_delivery_history
                (tenant_id, delivery_id, kind, attempt, error)
                SELECT tenant_id, delivery_id, 'recovered', attempts, error FROM changed
            """,
            (
                tenant,
                destination,
                destination,
                MAX_ATTEMPTS,
                destination,
                destination,
                MAX_ATTEMPTS,
            ),
        )

    async def claim(self, tenant: str, destination: str) -> dict[str, Any] | None:
        return await self.pool.fetchrow(
            """
            WITH candidate AS (
                SELECT tenant_id, delivery_id FROM alert_deliveries
                WHERE tenant_id = %s AND status = 'pending' AND next_attempt_at <= now()
                    AND destination_url = %s AND cycle_attempts < %s
                ORDER BY next_attempt_at, created_at LIMIT 1 FOR UPDATE SKIP LOCKED
            ), claimed AS (
                UPDATE alert_deliveries d SET status = 'sending', lease_token = %s,
                    lease_until = now() + %s * interval '1 second',
                    attempts = attempts + 1, cycle_attempts = cycle_attempts + 1,
                    next_attempt_at = NULL, updated_at = now()
                FROM candidate c WHERE d.tenant_id = c.tenant_id AND d.delivery_id = c.delivery_id
                RETURNING d.*
            ), history AS (
                INSERT INTO alert_delivery_history (tenant_id, delivery_id, kind, attempt)
                SELECT tenant_id, delivery_id, 'sending', attempts FROM claimed
            ) SELECT * FROM claimed
            """,
            (tenant, destination, MAX_ATTEMPTS, uuid4().hex, LEASE_S),
        )

    async def finish(
        self, row: dict[str, Any], *, status_code: int | None, error: str | None
    ) -> str:
        delivered = error is None
        retryable = status_code is None or status_code in {408, 425, 429} or status_code >= 500
        retry = not delivered and retryable and row["cycle_attempts"] < MAX_ATTEMPTS
        status = "delivered" if delivered else "pending" if retry else "failed"
        changed = await self.pool.fetchrow(
            """
            WITH changed AS (
                UPDATE alert_deliveries SET status = %s, status_code = %s, error = %s,
                    delivered_at = CASE WHEN %s THEN now() ELSE delivered_at END,
                    next_attempt_at = CASE WHEN %s THEN now() + %s * interval '1 second' ELSE NULL END,
                    lease_token = NULL, lease_until = NULL, updated_at = now()
                WHERE tenant_id = %s AND delivery_id = %s AND status = 'sending' AND lease_token = %s
                RETURNING *
            ), history AS (
                INSERT INTO alert_delivery_history
                    (tenant_id, delivery_id, kind, attempt, status_code, error)
                SELECT tenant_id, delivery_id, %s, attempts, status_code, error FROM changed
            ) SELECT delivery_id FROM changed
            """,
            (
                status,
                status_code,
                error,
                delivered,
                retry,
                retry_delay(row["cycle_attempts"]),
                row["tenant_id"],
                row["delivery_id"],
                row["lease_token"],
                "delivered" if delivered else "attempt_failed",
            ),
        )
        return status if changed else "lease_lost"

    async def dispatch_one(self, tenant: str, destination: str) -> str | None:
        row = await self.claim(tenant, destination)
        if not row:
            return None
        code = None
        error = None
        try:
            # Total deadline (including slow streaming responses) stays below the lease.
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                async with self.client.stream(
                    "POST",
                    row["destination_url"],
                    json=row["payload"],
                    follow_redirects=False,
                    headers={
                        "Idempotency-Key": row["delivery_id"],
                        "X-SIO-Delivery-ID": row["delivery_id"],
                    },
                ) as response:
                    code = response.status_code
                    if not 200 <= code < 300:
                        error = f"Receiver returned HTTP {code}."
        except (TimeoutError, httpx.TimeoutException):
            error = "Delivery timed out; receiver acceptance is unknown."
        except (httpx.HTTPError, httpx.InvalidURL):
            error = "Endpoint could not be reached."
        # Never store receiver bodies, exception messages or URLs: all may contain secrets.
        return await self.finish(row, status_code=code, error=error)

    async def dispatch(self, tenant: str, destination: str) -> list[str | None]:
        await self.recover(tenant, destination)
        if not destination:
            return []
        return list(
            await asyncio.gather(
                *(self.dispatch_one(tenant, destination) for _ in range(CONCURRENCY))
            )
        )

    async def list(
        self,
        tenant: str,
        destination: str,
        *,
        status: str | None = None,
        alert_id: str | None = None,
        limit: int = 100,
        allowed_zones: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        rows = await self.pool.fetch(
            """
            SELECT d.*, COALESCE(h.history, '[]'::jsonb) AS history FROM alert_deliveries d
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(to_jsonb(recent) ORDER BY at DESC, history_id DESC) AS history
                FROM (SELECT history_id, kind, at, attempt, status_code, error, actor, reason
                    FROM alert_delivery_history h
                    WHERE h.tenant_id = d.tenant_id AND h.delivery_id = d.delivery_id
                    ORDER BY history_id DESC LIMIT 20) recent
            ) h ON true
            WHERE d.tenant_id = %s AND (%s::text IS NULL OR d.status = %s)
                AND (%s::text IS NULL OR d.alert_id = %s)
                AND (cardinality(%s::text[]) = 0 OR d.payload->>'zone_id' IS NULL
                    OR d.payload->>'zone_id' = ANY(%s::text[]))
            ORDER BY d.created_at DESC, d.delivery_id DESC LIMIT %s
            """,
            (
                tenant,
                status,
                status,
                alert_id,
                alert_id,
                list(allowed_zones),
                list(allowed_zones),
                limit,
            ),
        )
        return {
            "configured": bool(destination),
            "destination": destination_label(destination),
            "max_attempts": MAX_ATTEMPTS,
            "deliveries": [self.public(row, destination) for row in rows],
        }

    @staticmethod
    def public(row: dict[str, Any], destination: str) -> dict[str, Any]:
        keys = (
            "delivery_id",
            "alert_id",
            "action",
            "title",
            "status",
            "attempts",
            "cycle_attempts",
            "created_at",
            "updated_at",
            "next_attempt_at",
            "delivered_at",
            "status_code",
            "error",
        )
        return {
            **{key: row.get(key) for key in keys},
            "max_attempts": MAX_ATTEMPTS,
            "destination": destination_label(row["destination_url"]),
            "can_retry": row["status"] in {"failed", "blocked"}
            and bool(destination)
            and row["destination_url"] == destination,
            "history": row.get("history", []),
        }

    async def retry(
        self,
        tenant: str,
        identifier: str,
        destination: str,
        actor: str,
        reason: str,
        *,
        allowed_zones: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            WITH changed AS (
                UPDATE alert_deliveries SET status = 'pending', cycle_attempts = 0,
                    next_attempt_at = now(), updated_at = now(), error = NULL, status_code = NULL
                WHERE tenant_id = %s AND delivery_id = %s AND status IN ('failed', 'blocked')
                    AND destination_url = %s AND %s <> ''
                    AND (cardinality(%s::text[]) = 0 OR payload->>'zone_id' IS NULL
                        OR payload->>'zone_id' = ANY(%s::text[]))
                RETURNING *
            ), history AS (
                INSERT INTO alert_delivery_history (tenant_id, delivery_id, kind, attempt, actor, reason)
                SELECT tenant_id, delivery_id, 'manual_retry', attempts, %s, %s FROM changed
            ) SELECT * FROM changed
            """,
            (
                tenant,
                identifier,
                destination,
                destination,
                list(allowed_zones),
                list(allowed_zones),
                actor,
                reason,
            ),
        )
        if row is None:
            exists = await self.pool.fetchrow(
                "SELECT delivery_id FROM alert_deliveries WHERE tenant_id = %s AND delivery_id = %s "
                "AND (cardinality(%s::text[]) = 0 OR payload->>'zone_id' IS NULL "
                "OR payload->>'zone_id' = ANY(%s::text[]))",
                (tenant, identifier, list(allowed_zones), list(allowed_zones)),
            )
            if not exists:
                raise HTTPException(404, "Delivery not found.")
            raise HTTPException(
                409,
                "Only failed or blocked deliveries to the unchanged configured endpoint can be retried.",
            )
        return self.public(row, destination)
