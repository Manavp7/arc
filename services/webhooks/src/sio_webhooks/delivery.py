"""Outbound delivery, with retries that cannot stall the pipeline (PRD M22, Phase 6).

Three properties, each chosen because its absence is a failure this platform has already had once elsewhere.

**Delivery is decoupled from consumption.** The bus consumer records a pending delivery and returns; a worker
does the HTTP. If the consumer awaited the request, one endpoint taking thirty seconds would stall the topic for
every other subscriber — and a third party's outage would become the platform's. The alerts service learned a
smaller version of this lesson with its single webhook, which needed a circuit breaker for the same reason.

**A retry schedule with a ceiling and an end.** Exponential backoff with jitter, five attempts, then the
delivery is `failed` and stays failed. Retrying forever means a permanently broken endpoint accumulates work
that never drains, and the queue becomes a monument to a URL somebody deleted last March.

**Which failures are worth retrying is a decision, not a default.** A 500 is worth retrying; a 404 or a 401 is
the endpoint telling you something that will not change on its own, and hammering it for twenty minutes is rude
and pointless. `should_retry` encodes that, with the reasoning next to it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

#: How many times a delivery is attempted in total, including the first.
#:
#: Five attempts over roughly nine minutes with the backoff below. Enough to ride out a deploy or a restart,
#: short enough that a genuinely dead endpoint is marked failed while somebody still remembers configuring it.
MAX_ATTEMPTS = 5

#: Base and ceiling for exponential backoff, in seconds.
#:
#: The ceiling matters more than the base. Without it, attempt 8 is two hours out, which means a delivery can
#: outlive the incident it describes — and a webhook that arrives after the fire is out is worse than none,
#: because somebody will act on it.
BASE_DELAY_S = 5.0
MAX_DELAY_S = 300.0

#: Status codes worth another attempt.
#:
#: 408 request timeout, 425 too early, 429 rate limited, and the 5xx family. Everything else is the receiver
#: stating a fact about the request that will still be true in five minutes.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})


@dataclass
class Attempt:
    """One delivery attempt, and what came back."""

    number: int
    ok: bool
    status_code: int | None = None
    error: str | None = None
    duration_ms: float = 0.0

    def describe(self) -> dict[str, Any]:
        return {
            "attempt": self.number,
            "ok": self.ok,
            "status_code": self.status_code,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass
class Delivery:
    """One event on its way to one subscriber."""

    delivery_id: str
    webhook_id: str
    tenant_id: str
    url: str
    topic: str
    body: bytes
    secret: str | None = None
    message_id: str | None = None
    event_kind: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    status: str = "pending"
    next_retry_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    delivered_at: datetime | None = None

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def last(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def exhausted(self) -> bool:
        return self.attempt_count >= MAX_ATTEMPTS

    def record(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)
        if attempt.ok:
            self.status = "delivered"
            self.delivered_at = datetime.now(UTC)
            self.next_retry_at = None
            return
        if not should_retry(attempt) or self.exhausted:
            self.status = "failed"
            self.next_retry_at = None
            return
        self.status = "pending"
        self.next_retry_at = datetime.now(UTC) + timedelta(
            seconds=backoff_delay(self.attempt_count)
        )

    def describe(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "webhook_id": self.webhook_id,
            "url": self.url,
            "topic": self.topic,
            "event_kind": self.event_kind,
            "status": self.status,
            "attempts": [attempt.describe() for attempt in self.attempts],
            "created_at": self.created_at.isoformat(),
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            # Said explicitly, because "failed" alone leaves the reader to guess whether it will be retried.
            "will_retry": self.status == "pending" and not self.exhausted,
        }


def should_retry(attempt: Attempt) -> bool:
    """Whether a failure is worth another attempt.

    A connection error is always worth retrying: the endpoint may be restarting, and there is no statement from
    the receiver to respect. A *response* is a statement — 404 means the path is wrong, 401 means the secret is
    wrong, 400 means the body is wrong, and none of those changes because we asked again nine minutes later.
    Retrying them is both futile and impolite to somebody else's server.
    """
    if attempt.status_code is None:
        return True
    return attempt.status_code in RETRYABLE_STATUS


def backoff_delay(attempt_number: int, *, jitter: bool = True) -> float:
    """Seconds to wait before attempt `attempt_number + 1`.

    Jittered, and this is not decoration. Without jitter, a platform with two hundred subscribers to one topic
    retries all two hundred at exactly the same instant after an outage — so the receiver's first moment back
    online is its worst, and it falls over again. Full jitter (uniform over the whole interval) rather than
    equal jitter, because the herd here can be large relative to the interval.
    """
    delay = min(MAX_DELAY_S, BASE_DELAY_S * (2 ** max(0, attempt_number - 1)))
    return random.uniform(0, delay) if jitter else delay


def matches(
    topics: list[str],
    topic: str,
    event_kind: str | None = None,
    event_type: str | None = None,
) -> bool:
    """Whether a subscription wants this message.

    Four forms, because subscribers think in different granularities:

    * `*` — everything;
    * `alerts` — a bus topic;
    * `alerts.*` — a family of topics;
    * `fire_detected` — a specific **event type**.

    The last one needs `event_type` and that is the whole point of this signature. The first version took only
    the message KIND — the schema class name, `"Event"` or `"Alert"` — and compared subscriptions against it. So
    subscribing to `fire_detected` matched nothing, silently, while the API's own help text and this docstring
    both advertised it as supported. My own test table caught it: `topics=['fire_detected']` against an
    `Event` on the `events` topic returned False.

    Supporting only topics would force anybody interested in one event type to receive every event and filter
    client-side, which is precisely the traffic a webhook exists to avoid.
    """
    if not topics:
        # An empty list means nothing, not everything. A subscription created without topics should be inert
        # rather than a firehose — the failure of the first interpretation is a surprised subscriber, and of the
        # second, a subscriber's server melting.
        return False
    for wanted in topics:
        if wanted == "*":
            return True
        if wanted == topic:
            return True
        if event_kind and wanted == event_kind:
            return True
        if event_type and wanted == event_type:
            return True
        # `alerts.*` style prefixes, for a subscriber who wants a family.
        if wanted.endswith(".*") and topic.startswith(wanted[:-1]):
            return True
    return False


__all__ = [
    "BASE_DELAY_S",
    "MAX_ATTEMPTS",
    "MAX_DELAY_S",
    "RETRYABLE_STATUS",
    "Attempt",
    "Delivery",
    "backoff_delay",
    "matches",
    "should_retry",
]
