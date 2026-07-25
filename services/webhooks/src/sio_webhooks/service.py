"""Webhook fan-out (PRD M22, Phase 6).

Subscribes to the topics a deployment cares about, matches each message against the active subscriptions, and
delivers with a signature, retries and a log.

The relationship with the alerts service's webhook is worth stating: that one posts a single configured URL when
an alert is raised, and it stays. This is the general mechanism — many subscribers, many topics, signed,
retried, logged. Alerts kept its own because a critical alert should not depend on a second service being up,
and duplicating a POST is cheaper than a dependency in that direction.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from sio_core import MessageContext, PgPool, SioService, describe_error, get_pg_pool
from sio_schemas import BusMessage, Topic, WebhookSubscription, new_id

from .delivery import MAX_ATTEMPTS, Attempt, Delivery, matches
from .signing import ATTEMPT_HEADER, DELIVERY_HEADER, SIGNATURE_HEADER, TOPIC_HEADER, sign

#: How long a single POST may take.
#:
#: Ten seconds. A webhook receiver that needs longer is doing work it should be queueing, and waiting for it
#: makes this service's throughput a function of somebody else's architecture.
POST_TIMEOUT_S = 10.0

#: How many deliveries run at once.
#:
#: Bounded, because a burst of events across many subscribers would otherwise open unbounded connections — and
#: the first thing that breaks is this service, not the receivers.
CONCURRENCY = 8

#: The topics forwarded. Everything a subscriber could plausibly want, and nothing raw.
#:
#: Raw frames and GPS fixes are deliberately excluded: they are high-volume, low-meaning, and a webhook is the
#: wrong transport for thirty messages a second. A subscriber who wants raw data wants the bus.
FORWARDED = (Topic.EVENTS, Topic.ALERTS, Topic.DECISIONS, Topic.ACTIONS, Topic.SIMULATIONS)


class SubscriptionRequest(BaseModel):
    url: str
    topics: list[str] = Field(default_factory=list)
    secret: str | None = None
    active: bool = True


class WebhooksService(SioService):
    """Signs, delivers, retries and records."""

    name = "webhooks"
    subscribes = FORWARDED
    tick_interval_s = 5.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self.client = httpx.AsyncClient(timeout=POST_TIMEOUT_S)
        self._semaphore = asyncio.Semaphore(CONCURRENCY)
        self._queued = 0
        self._delivered = 0
        self._failed = 0
        self._retried = 0
        self._workers: set[asyncio.Task[None]] = set()

    async def setup(self) -> None:
        await self.pool.open()
        count = await self.pool.fetchval(
            "SELECT count(*) FROM webhooks WHERE tenant_id = %s AND active",
            (self.settings.tenant_id,),
        )
        self.log.info(
            "webhooks.ready",
            active_subscriptions=int(count or 0),
            forwarding=[str(topic) for topic in FORWARDED],
            max_attempts=MAX_ATTEMPTS,
        )

    async def teardown(self) -> None:
        # Let in-flight deliveries finish rather than cancelling them. A delivery cancelled mid-POST leaves the
        # receiver having possibly processed it and the log saying pending, which is the one state nobody can
        # resolve afterwards.
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        await self.client.aclose()

    async def health_checks(self) -> dict[str, str]:
        checks = {"postgres": "ok" if await self.pool.ping() else "unreachable"}
        stuck = await self.pool.fetchval(
            """
            SELECT count(*) FROM webhook_deliveries
             WHERE tenant_id = %s AND status = 'pending'
               AND created_ts < now() - interval '30 minutes'
            """,
            (self.settings.tenant_id,),
        )
        if int(stuck or 0):
            # Pending for half an hour means the retry sweep is not draining, which is invisible from the
            # counters alone — they would show deliveries being attempted, just never finishing.
            checks["retry_queue"] = f"degraded: {stuck} delivery(ies) pending for over 30 minutes"
        return checks

    async def health_info(self) -> dict[str, str]:
        return {
            "queued": str(self._queued),
            "delivered": str(self._delivered),
            "failed": str(self._failed),
            "retried": str(self._retried),
            "in_flight": str(len(self._workers)),
        }

    # ------------------------------------------------------------------ fan-out
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        """Record a pending delivery per matching subscription, and return.

        The HTTP happens in a worker. If this awaited the POST, one endpoint taking thirty seconds would stall
        the topic for every other subscriber — and somebody else's outage would become the platform's.
        """
        subscriptions = await self._active_subscriptions()
        if not subscriptions:
            return

        body = message.to_json().encode()
        # The event TYPE, from the payload, so a subscription to `fire_detected` works. The message kind is the
        # schema class — `Event`, `Alert` — which is a coarser thing entirely.
        event_type = _event_type_of(message)
        for subscription in subscriptions:
            if not matches(subscription.topics, str(message.topic), message.kind, event_type):
                continue
            delivery = Delivery(
                delivery_id=new_id("dlv"),
                webhook_id=subscription.webhook_id,
                tenant_id=subscription.tenant_id,
                url=subscription.url,
                topic=str(message.topic),
                body=body,
                secret=subscription.secret,
                message_id=message.id,
                event_kind=message.kind,
            )
            await self._record(delivery)
            self._queued += 1
            self._spawn(delivery)

    def _spawn(self, delivery: Delivery) -> None:
        task = asyncio.create_task(self._attempt(delivery), name=f"deliver-{delivery.delivery_id}")
        # Held in a set, because an un-referenced task can be garbage-collected mid-flight — the same bug the
        # supervisor's output pumps had, where a dropped reference silently stopped tailing a service.
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    async def _attempt(self, delivery: Delivery) -> None:
        async with self._semaphore:
            started = time.perf_counter()
            number = delivery.attempt_count + 1
            headers = {
                "content-type": "application/json",
                DELIVERY_HEADER: delivery.delivery_id,
                TOPIC_HEADER: delivery.topic,
                ATTEMPT_HEADER: str(number),
                "user-agent": "sio-webhooks/0.1",
            }
            if delivery.secret:
                headers[SIGNATURE_HEADER] = sign(delivery.body, delivery.secret)

            try:
                response = await self.client.post(
                    delivery.url, content=delivery.body, headers=headers
                )
                elapsed = (time.perf_counter() - started) * 1000
                ok = 200 <= response.status_code < 300
                attempt = Attempt(
                    number=number,
                    ok=ok,
                    status_code=response.status_code,
                    error=None if ok else response.text[:200],
                    duration_ms=elapsed,
                )
            except Exception as exc:
                attempt = Attempt(
                    number=number,
                    ok=False,
                    error=describe_error(exc),
                    duration_ms=(time.perf_counter() - started) * 1000,
                )

            delivery.record(attempt)
            await self._record(delivery)

            if attempt.ok:
                self._delivered += 1
                await self._mark_subscription(delivery.webhook_id, error=None)
            elif delivery.status == "failed":
                self._failed += 1
                await self._mark_subscription(delivery.webhook_id, error=attempt.error)
                self.log.warning(
                    "webhooks.gave_up",
                    delivery=delivery.delivery_id,
                    url=delivery.url,
                    attempts=delivery.attempt_count,
                    last_error=attempt.error,
                    status_code=attempt.status_code,
                )
            else:
                await self._mark_subscription(delivery.webhook_id, error=attempt.error)

    async def tick(self) -> None:
        """Retry sweep: pick up deliveries whose backoff has elapsed.

        A sweep rather than a sleeping task per delivery, so retries survive a restart. A `asyncio.sleep` in a
        worker loses everything queued when the process stops, which is exactly when a retry queue matters —
        the deploy that broke the receiver is often the one that restarted this service too.
        """
        rows = await self.pool.fetch(
            """
            SELECT delivery_id, webhook_id, url, topic, message_id, event_kind, attempts, body_preview
              FROM webhook_deliveries
             WHERE tenant_id = %s AND status = 'pending'
               AND next_retry_ts IS NOT NULL AND next_retry_ts <= now()
             ORDER BY next_retry_ts
             LIMIT 20
            """,
            (self.settings.tenant_id,),
        )
        if not rows:
            return

        subscriptions = {sub.webhook_id: sub for sub in await self._active_subscriptions()}
        for row in rows:
            subscription = subscriptions.get(str(row["webhook_id"]))
            if subscription is None:
                # The subscription was deleted or deactivated while a delivery was pending. Dropped rather
                # than retried: continuing to POST to a webhook somebody removed is the behaviour that makes
                # people distrust a delivery system.
                await self.pool.execute(
                    """
                    UPDATE webhook_deliveries SET status = 'dropped',
                           error = 'the subscription was removed or deactivated before this could be retried'
                     WHERE tenant_id = %s AND delivery_id = %s
                    """,
                    (self.settings.tenant_id, row["delivery_id"]),
                )
                continue

            delivery = Delivery(
                delivery_id=str(row["delivery_id"]),
                webhook_id=str(row["webhook_id"]),
                tenant_id=self.settings.tenant_id,
                url=str(row["url"]),
                topic=str(row["topic"]),
                # The stored preview, not the original body. A retry is signed afresh over what is on record,
                # which keeps the log the authoritative account of what was sent — if the two could differ, the
                # log would be a plausible fiction.
                body=str(row["body_preview"] or "{}").encode(),
                secret=subscription.secret,
                message_id=row["message_id"],
                event_kind=row["event_kind"],
            )
            delivery.attempts = [
                Attempt(number=index + 1, ok=False) for index in range(int(row["attempts"] or 0))
            ]
            self._retried += 1
            self._spawn(delivery)

    # -------------------------------------------------------------- persistence
    async def _active_subscriptions(self) -> list[WebhookSubscription]:
        rows = await self.pool.fetch(
            """
            SELECT tenant_id, webhook_id, url, topics, secret, active, created_ts,
                   failure_count, last_delivery_ts, last_error
              FROM webhooks WHERE tenant_id = %s AND active
            """,
            (self.settings.tenant_id,),
        )
        return [WebhookSubscription.model_validate(dict(row)) for row in rows]

    async def _record(self, delivery: Delivery) -> None:
        last = delivery.last
        await self.pool.execute(
            """
            INSERT INTO webhook_deliveries (
                tenant_id, delivery_id, webhook_id, topic, message_id, event_kind, url, status,
                attempts, status_code, error, duration_ms, created_ts, delivered_ts, next_retry_ts,
                body_preview
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, delivery_id) DO UPDATE SET
                status        = EXCLUDED.status,
                attempts      = EXCLUDED.attempts,
                status_code   = EXCLUDED.status_code,
                error         = EXCLUDED.error,
                duration_ms   = EXCLUDED.duration_ms,
                delivered_ts  = EXCLUDED.delivered_ts,
                next_retry_ts = EXCLUDED.next_retry_ts
            """,
            (
                delivery.tenant_id,
                delivery.delivery_id,
                delivery.webhook_id,
                delivery.topic,
                delivery.message_id,
                delivery.event_kind,
                delivery.url,
                delivery.status,
                delivery.attempt_count,
                last.status_code if last else None,
                last.error if last else None,
                last.duration_ms if last else None,
                delivery.created_at,
                delivery.delivered_at,
                delivery.next_retry_at,
                delivery.body.decode(errors="replace")[:4000],
            ),
        )

    async def _mark_subscription(self, webhook_id: str, *, error: str | None) -> None:
        """Update the subscription's own health.

        `failure_count` resets on success rather than accumulating for ever: the number an operator wants is
        "is this broken now", and a lifetime total answers a question nobody asks while hiding the one they do.
        """
        if error is None:
            await self.pool.execute(
                """
                UPDATE webhooks SET failure_count = 0, last_delivery_ts = now(), last_error = NULL
                 WHERE tenant_id = %s AND webhook_id = %s
                """,
                (self.settings.tenant_id, webhook_id),
            )
        else:
            await self.pool.execute(
                """
                UPDATE webhooks SET failure_count = failure_count + 1, last_error = %s
                 WHERE tenant_id = %s AND webhook_id = %s
                """,
                (error[:500], self.settings.tenant_id, webhook_id),
            )

    # -------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/webhooks", tags=["webhooks"])
        async def list_webhooks() -> dict[str, Any]:
            rows = await self.pool.fetch(
                """
                SELECT webhook_id, url, topics, active, created_ts, failure_count,
                       last_delivery_ts, last_error, (secret IS NOT NULL) AS signed
                  FROM webhooks WHERE tenant_id = %s ORDER BY created_ts DESC
                """,
                (self.settings.tenant_id,),
            )
            # The secret is never returned, only whether one is set. A secret that can be read back is a secret
            # that ends up in a screenshot.
            return {"webhooks": [dict(row) for row in rows]}

        @app.post("/webhooks", tags=["webhooks"])
        async def create(request: SubscriptionRequest) -> dict[str, Any]:
            if not request.url.startswith(("http://", "https://")):
                raise HTTPException(
                    status_code=400, detail="the url must begin with http:// or https://"
                )
            if not request.topics:
                # Refused rather than defaulted. An empty topic list is inert by design (see `matches`), and
                # silently creating a subscription that will never fire wastes somebody's afternoon.
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "at least one topic is required; use '*' for everything, a bus topic like 'alerts', "
                        "or an event type like 'fire_detected'"
                    ),
                )
            subscription = WebhookSubscription(
                tenant_id=self.settings.tenant_id,
                url=request.url,
                topics=request.topics,
                secret=request.secret,
                active=request.active,
            )
            await self.pool.execute(
                """
                INSERT INTO webhooks (tenant_id, webhook_id, url, topics, secret, active, created_ts)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    subscription.tenant_id,
                    subscription.webhook_id,
                    subscription.url,
                    subscription.topics,
                    subscription.secret,
                    subscription.active,
                    subscription.created_ts,
                ),
            )
            self.log.info(
                "webhooks.created",
                webhook=subscription.webhook_id,
                url=subscription.url,
                topics=subscription.topics,
                signed=bool(subscription.secret),
            )
            return {
                "webhook_id": subscription.webhook_id,
                "url": subscription.url,
                "topics": subscription.topics,
                "signed": bool(subscription.secret),
                "note": (
                    "Deliveries carry X-SIO-Signature as 't=<unix>,v1=<hmac-sha256>' over "
                    "'<t>.<body>'. Verify the timestamp as well as the digest, or a captured "
                    "delivery can be replayed indefinitely."
                )
                if subscription.secret
                else (
                    "No secret was set, so deliveries are UNSIGNED and a receiver cannot tell them from "
                    "anybody else's POST. Set one unless this endpoint is unreachable from outside."
                ),
            }

        @app.delete("/webhooks/{webhook_id}", tags=["webhooks"])
        async def delete(webhook_id: str) -> dict[str, Any]:
            deleted = await self.pool.execute(
                "DELETE FROM webhooks WHERE tenant_id = %s AND webhook_id = %s",
                (self.settings.tenant_id, webhook_id),
            )
            if not deleted:
                raise HTTPException(status_code=404, detail=f"unknown webhook {webhook_id!r}")
            # The delivery log is kept. A history that vanishes with the subscription is useless exactly when
            # somebody is asking what happened before it was removed.
            return {"deleted": webhook_id, "deliveries_retained": True}

        @app.get("/webhooks/deliveries", tags=["webhooks"])
        async def deliveries(
            webhook_id: str | None = None,
            status: str | None = None,
            limit: int = Query(default=50, ge=1, le=500),
        ) -> dict[str, Any]:
            """The delivery log — the only question anybody asks about a webhook.

            A subscription row cannot answer "did it fire?", and without this the honest response to that
            question is to read the service's logs.
            """
            rows = await self.pool.fetch(
                """
                SELECT delivery_id, webhook_id, topic, event_kind, url, status, attempts,
                       status_code, error, duration_ms, created_ts, delivered_ts, next_retry_ts
                  FROM webhook_deliveries
                 WHERE tenant_id = %s
                   AND (%s::text IS NULL OR webhook_id = %s)
                   AND (%s::text IS NULL OR status = %s)
                 ORDER BY created_ts DESC
                 LIMIT %s
                """,
                (self.settings.tenant_id, webhook_id, webhook_id, status, status, limit),
            )
            summary = await self.pool.fetchrow(
                """
                SELECT count(*) FILTER (WHERE status = 'delivered') AS delivered,
                       count(*) FILTER (WHERE status = 'failed') AS failed,
                       count(*) FILTER (WHERE status = 'pending') AS pending,
                       count(*) FILTER (WHERE status = 'dropped') AS dropped,
                       avg(duration_ms) FILTER (WHERE status = 'delivered') AS mean_ms
                  FROM webhook_deliveries WHERE tenant_id = %s
                """,
                (self.settings.tenant_id,),
            )
            return {
                "deliveries": [dict(row) for row in rows],
                "summary": {
                    key: (round(float(value), 1) if key == "mean_ms" and value else int(value or 0))
                    for key, value in dict(summary or {}).items()
                },
            }

        @app.post("/webhooks/{webhook_id}/test", tags=["webhooks"])
        async def test(webhook_id: str) -> dict[str, Any]:
            """Send a test delivery, synchronously, and report exactly what happened.

            Synchronous on purpose, unlike real deliveries: somebody configuring a webhook wants to know
            immediately whether the URL and the secret are right, and "queued" is not an answer to that.
            """
            row = await self.pool.fetchrow(
                "SELECT webhook_id, url, topics, secret FROM webhooks "
                " WHERE tenant_id = %s AND webhook_id = %s",
                (self.settings.tenant_id, webhook_id),
            )
            if row is None:
                raise HTTPException(status_code=404, detail=f"unknown webhook {webhook_id!r}")

            delivery = Delivery(
                delivery_id=new_id("dlv"),
                webhook_id=str(row["webhook_id"]),
                tenant_id=self.settings.tenant_id,
                url=str(row["url"]),
                topic="test",
                body=(
                    b'{"kind":"WebhookTest","message":"If you can read this, the URL and signature are '
                    b'correct."}'
                ),
                secret=row["secret"],
                event_kind="WebhookTest",
            )
            await self._record(delivery)
            await self._attempt(delivery)
            return delivery.describe()


def _event_type_of(message: BusMessage) -> str | None:
    """The `type` field inside the payload, when there is one.

    Read defensively: not every message has a type, the payload may be a string or a model depending on how the
    message was built, and a webhook fan-out that raises on an unexpected shape would stall the topic for
    everybody. A subscription by event type is a nicety; delivering the other subscriptions is not.
    """
    payload = getattr(message, "payload", None)
    if isinstance(payload, dict):
        value = payload.get("type")
        return str(value) if value else None
    value = getattr(payload, "type", None)
    return str(value) if value else None


__all__ = ["CONCURRENCY", "FORWARDED", "POST_TIMEOUT_S", "WebhooksService"]
