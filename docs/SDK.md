# SDK

A typed Python client. `pip install sio-sdk`, or from this repo it is already in the workspace.

```python
from sio_sdk import SioClient

async with SioClient() as sio:
    for entity in await sio.entities(limit=10):
        print(entity.label, entity.state.zone_id)

    print(await sio.ask("What is on site right now?"))
```

## The quickstart runs

The plan's acceptance for this is "the quickstart in docs/SDK.md runs", so it is a **script in the repository**
rather than prose here: [`examples/sdk_quickstart.py`](../examples/sdk_quickstart.py).

```bash
just services && just dev          # a running platform
uv run python examples/sdk_quickstart.py
```

A test asserts this document and that script cannot drift apart. A quickstart pasted into documentation stops
running the first time an API changes, while still looking correct — which is worse than not having one, because
it is the first thing a new user tries.

Real output, from a running stack:

```
connected as 'quickstart' in tenant 'default', roles ['commander', 'operator']

33 moving entities: {'truck': 19, 'person': 9, 'forklift': 3, 'drone': 2}
  e.g. Forklift 9 (forklift) in lane_north

5 alerts needing attention:
  priority   60.4  Flame detected by cam-fuel with confidence 0.804
                  critical severity, 70% confidence, fuel_store is a critical area (x2), 30 occurrences

risk: 66.1 / 100 (elevated)
  - open criticals: 4 critical alert(s) open; 3 or more saturates this term
  dwell: long right tail: p95 (14.2) is 24.8x the median (0.6), so the mean of 2.4 describes almost nobody

what-if: A fire in Fuel store would reach about 180 m downwind and 90 m across wind within 30 min
  impact: {'entities_at_risk': 27.0, 'people_at_risk': 7.0, 'zones_at_risk': 9.0}
  (a projection against a frozen copy of the world; nothing on site changed)

asked: What is on site right now?
  There are 33 moving entities on site in the last 5 minutes...
  via ['list_entities'] at 70% confidence
  Personal data was removed from this response (2 us plate)

streaming three live messages:
  Event: zone_exited
  Event: zone_entered
```

---

## What the SDK is for

Not wrapping `httpx` in a class. It absorbs four things that are tedious and easy to get wrong, and each is a
mistake made while building this platform's own clients.

### Tokens

Every endpoint needs one, they expire, and a 401 mid-session is not a programming error — it is Tuesday. The
client obtains a token, reuses it, and renews on a 401 **exactly once**.

Once, not in a loop: a misconfigured secret would otherwise become a request storm against the token endpoint,
which the browser console's first version did. Minting is behind a lock, so five concurrent requests on a cold
client mint one token rather than five — which is wasteful and makes the audit trail read as five sign-ins.

In a Keycloak deployment the dev issuer is disabled by design, so pass a token:

```python
SioClient(token=os.environ["SIO_TOKEN"])
```

### Typed returns

`entities()` returns `list[Entity]`, not `list[dict]`. A dict-returning client pushes
`row["state"]["geo"]["lat"]` into every caller, so a renamed field fails at the point of use with a `KeyError`
rather than at the boundary with something readable.

This platform hit the dict version of that problem twice: a hand-written TypeScript type that described what its
author *believed* the API returned, and a copilot tool reading `detection.frame_id`, which does not exist.

### Streaming

```python
async for message in sio.subscribe("events", "alerts"):
    print(message.kind, message.payload)
```

Reconnects with backoff. SSE is easy to get *almost* right, and one detail in particular:
**`EventSource.onmessage` fires only for frames with no `event:` name.** A reader handling only unnamed frames
receives nothing from a server that names them — this platform's own console shipped that bug, and it presented
as a live map that never updated. The SDK handles both.

A malformed frame is skipped rather than raised, because a caller iterating a live feed cannot recover from an
exception mid-loop and the next frame is probably fine.

### Errors that say what to do

The API returns a reason for every denial. A client that raises `HTTPError: 403` throws it away.

```python
try:
    await sio.approve(decision_id)
except SioApiError as error:
    if error.is_permission_error:
        print(error.detail)  # "decision.approve needs one of: admin, commander; you have operator"
        print(error.rule)  # "decision.approve"
```

---

## Defaults that reflect what you probably meant

`entities()` defaults to `active_within_s=300, include_static=False` — "things that are here and moving".

This platform **deletes nothing**, so an unfiltered query legitimately returns every entity that has ever
existed, including ones from a run last Tuesday that will never move again. An SDK whose default is "all of
history" produces a first experience of confusing volume, and the caller has no way to know the default was the
problem.

Pass `active_within_s=None` for the full record.

---

## The permission gate is not a UI affordance

`sio.approve()` is on the client, and it is governed by the same policy as the console: `decision.approve` needs
a **commander** with clearance 2. A client constructed with the default `("operator",)` gets a 403 with the
reason.

That is the correct outcome, and it is worth being explicit that the SDK cannot route around it. The service that
recommends cannot execute; the agent that executes accepts only an approved decision, and only a recent one. An
SDK is another caller, not a back door.

---

## Sync, for a script

```python
from sio_sdk import SyncSioClient

sio = SyncSioClient()
print(sio.ask("What is on site?"))
for alert in sio.alerts(limit=5):
    print(alert.score, alert.title)
```

Every method mirrors the async client except `subscribe` — a blocking infinite iterator in a script is a trap,
and somebody who wants a live feed is better served by four lines of `asyncio.run`.

---

## TypeScript

`sdk/ts`, with types **generated** from the API's OpenAPI schema.

```ts
import { SioClient } from "@sio/sdk";

const sio = new SioClient();
for (const entity of await sio.entities({ limit: 10 })) {
  console.log(entity.label, entity.state.zone_id);
}
```

```bash
cd sdk/ts
npx tsx examples/quickstart.mts     # against a running platform
npm run generate                    # regenerate from the live API
npm run typecheck
```

Real output:

```
29 entities: { drone: 2, truck: 13, person: 11, forklift: 3 }
  e.g. Drone 0018 (drone) in no zone

3 alerts, ranked:
  55.1  Worker 11 entered Fuel store
  54.9  Flame detected by cam-fuel with confidence 0.804

approval attempt reached the service: unknown decision 'dec_does_not_exist'

streaming three live messages (the part OpenAPI cannot generate):
  Event: anomaly_detected
  Alert: The tide gauge reported a water level above the co
```

That last line came from the **example plugin's** rule, which is a pleasing accident: an out-of-tree package's
rule reaching a generated TypeScript client over SSE exercises most of Phase 6 at once.

### Building this client found a real gap

**44 of the API's 50 routes published no response type at all.** The gateway forwards, and a forwarded body has
no declared schema, so `components["schemas"]["Alert"]` simply did not exist — any generated client, in any
language, got `unknown` for almost every endpoint. `AlertsResponse` and `DecisionsResponse` now publish the two
the SDKs expose, declared for documentation only: `response_model` would *filter* the forwarded body to the
declared fields, silently dropping anything a service added, which is precisely the bug a schema exists to
prevent.

The remaining 42 are a known gap rather than a solved problem. They work, and they are `unknown` to a generated
client.

### `subscribe()` is hand-written, because SSE is not in OpenAPI

`EventSource` is the obvious tool and it is wrong twice over: it **cannot send an `Authorization` header** (which
is why the console authenticates its stream by cookie), and **`onmessage` fires only for frames with no `event:`
name**, so a reader handling only `onmessage` receives nothing from a server that names its frames — the bug the
console shipped, presenting as a live map that never updated.

`sdk/ts/src/client.ts` handles both frame types, keeps the trailing partial line across chunk boundaries, skips
malformed frames rather than throwing, and reconnects with backoff.

### Not published to npm

Publishing means owning a version number against an API that is still moving. It is consumed from the workspace,
and `npm run generate` targets whatever version you are actually running — more honest than a version claiming
compatibility.

## Go

Deferred, with the same path: `openapi.json` plus `oapi-codegen`. Nothing in the API is Python-specific, and the
schema is generated from the same Pydantic models the SDK returns, so a Go client is a code-generation task
rather than a design one.

---

## Every endpoint

| method | returns |
|---|---|
| `entities(entity_type=, zone_id=, active_within_s=, limit=)` | `list[Entity]` |
| `entity(entity_id)` | `Entity` |
| `events(limit=, event_type=)` | `list[Event]` |
| `alerts(state=, limit=)` | `list[Alert]` |
| `zones()` | `list[dict]` |
| `decisions(approval=, limit=)` | `list[Decision]` |
| `approve(decision_id, option_id=)` | `dict` — needs a commander |
| `ask(question)` | `CopilotAnswer` with `.text`, `.tools_used`, `.was_redacted` |
| `simulate(scenario, **params)` | `dict` — a projection; changes nothing |
| `forecasts()` | `dict` |
| `analytics(hours=)` | `dict` |
| `subscribe(*topics)` | `AsyncIterator[StreamMessage]` |
| `request(method, path, ...)` | the escape hatch, authenticated |

`request()` exists so an endpoint the SDK has not wrapped is one line away rather than a fork. A client that can
only do what its author anticipated is one people abandon at the first gap.
