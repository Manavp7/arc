# webhooks (M22)

Outbound delivery: signed, retried, logged.

```
events / alerts / decisions / actions / simulations
        │
        ├─► match subscriptions ──► record pending delivery ──► worker POSTs
        │                                                          │
        └─► retry sweep (every 5s, survives restart) ◄──────────────┘
```

## Three things the obvious implementation gets wrong

### A body-only signature can be replayed for ever

Anyone who captures one valid delivery can resend it a year later and the receiver verifies it happily. For a
platform that dispatches drones that is not a theoretical concern.

So the signature covers a **timestamp and the body**, and a receiver rejects anything outside a five-minute
tolerance:

```
X-SIO-Signature: t=1784986224,v1=5257a869e7...
X-SIO-Delivery:  dlv_01KY...
X-SIO-Attempt:   2
```

The signed string is `f"{t}.{body}"` — with a literal dot, because without it `t=1` over body `23x` and `t=12`
over body `3x` concatenate to the same string and two different requests would share a signature. Small window,
closed by one character.

`v1` is a version tag present from the start. Adding one later means every receiver handles both formats during
a migration nobody planned.

**`verify()` ships with the platform** rather than being left to each receiver, because the parts people get
wrong — the timestamp, the constant-time compare, the tolerance — are exactly the parts that matter. A receiver
that verifies incorrectly is worse than one that does not verify at all: it believes it is protected.

A rejection reports the **age**, not "invalid signature". Somebody debugging clock skew needs to know the
signature was fine and the time was not.

### Retries need a ceiling and an end

Five attempts, exponential backoff from 5 s, **capped at 300 s**, with **full jitter**.

The cap matters more than the base: without one, attempt 8 is two hours out, so a delivery can outlive the
incident it describes — and a webhook that arrives after the fire is out is worse than none, because somebody
will act on it.

The jitter is not decoration. Without it, two hundred subscribers to one topic retry at the same instant after an
outage, so the receiver's first moment back online is its worst and it falls over again.

Retrying for ever makes the queue a monument to a URL somebody deleted last March, so after five attempts a
delivery is `failed` and stays failed.

### Not every failure is worth retrying

A connection error always retries — the endpoint may be restarting, and there is no statement from the receiver
to respect. A **response** is a statement: 404 means the path is wrong, 401 means the secret is wrong, 400 means
the body is wrong, and none of those changes because we asked again nine minutes later. Retrying them is futile
and impolite to somebody else's server.

Retryable: `408, 425, 429, 500, 502, 503, 504, 507, 509` and any connection failure.

## Delivery is decoupled from consumption

The bus consumer records a pending delivery and returns; a worker does the HTTP. If the consumer awaited the
POST, one endpoint taking thirty seconds would stall the topic for **every other subscriber** — somebody else's
outage becoming the platform's.

Concurrency is bounded at 8, because a burst across many subscribers would otherwise open unbounded connections,
and the first thing that breaks is then this service rather than the receivers.

The retry sweep is a **query, not a sleeping task**, so retries survive a restart. `asyncio.sleep` in a worker
loses everything queued when the process stops — which is exactly when a retry queue matters, since the deploy
that broke the receiver is often the one that restarted this service.

## The delivery log is the product

"Did my webhook fire?" is the only question anybody asks about a webhook, and a subscription row cannot answer
it. Without the log the honest response is "read the service's logs".

`GET /webhooks/deliveries` — filterable by webhook and status, with a summary including mean delivery time.

The log is **not append-only**, unlike `audit_log`: attempt 1 failed, attempt 2 failed, attempt 3 succeeded is
*one* delivery with a history, not three deliveries. A row per attempt would make every query a `GROUP BY`, and
what an operator wants is one line per event with its outcome.

Deliveries are **kept when a subscription is deleted**, because a history that vanishes with the thing it
describes is useless precisely when somebody is asking what happened before it was removed. A delivery still
pending when its subscription goes away is `dropped`, not retried — continuing to POST to a webhook somebody
removed is the behaviour that makes people distrust a delivery system.

`failure_count` on the subscription **resets on success**. The number an operator wants is "is this broken now";
a lifetime total answers a question nobody asks while hiding the one they do.

## Subscribing

Four granularities, because subscribers think in different ones:

| topic | matches |
|---|---|
| `*` | everything forwarded |
| `alerts` | a bus topic |
| `alerts.*` | a family of topics |
| `fire_detected` | a specific **event type** |

The last one needed the payload's `type`, not the message kind. The first version compared subscriptions against
the schema class name — `"Event"` — so subscribing to `fire_detected` matched **nothing, silently**, while the
API's own help text advertised it. My own test table caught it.

**An empty topic list receives nothing**, not everything, and the API refuses to create such a subscription:
the failure of this interpretation is a surprised subscriber, and of the other, a subscriber's server melting.

**Raw topics are not forwarded.** Thirty GPS fixes a second is not a webhook workload, and forwarding it would
make the first `*` subscription an accidental denial-of-service against its own receiver. A subscriber who wants
raw data wants the bus.

## Endpoints

| | |
|---|---|
| `POST /webhooks` | create; returns whether it is signed and how to verify |
| `GET /webhooks` | list — **never returns the secret**, only whether one is set |
| `DELETE /webhooks/{id}` | remove; the delivery log is retained |
| `POST /webhooks/{id}/test` | send a test delivery **synchronously** and report exactly what happened |
| `GET /webhooks/deliveries` | the log |

The test endpoint is synchronous unlike real deliveries, because somebody configuring a webhook wants to know
now whether the URL and the secret are right, and "queued" is not an answer to that.

## Why the alerts service still has its own webhook

`services/alerts` posts a single configured URL when an alert is raised, and it stays. A critical alert should
not depend on a second service being up, and duplicating one POST is cheaper than a dependency in that
direction. This service is the general mechanism: many subscribers, many topics, signed, retried, logged.
