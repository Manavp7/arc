# alerts (M16)

A prioritised, deduplicated, escalatable inbox.

```
events    ──┐
decisions ──┴─► alerts ──► alerts topic ──► the console
                       └─► postgres (durable), webhook (convenience)
```

## An inbox lives or dies on its ordering

Get it wrong and the inbox becomes a list nobody reads — at which point the alerting is **worse than none**,
because it has converted a real signal into noise people have learned to dismiss.

The score has four factors, kept separate so the ranking can be *explained* rather than merely produced.
Every alert carries the sentence that justifies its position, because "why is this at the top" is the first
question an operator asks and "the algorithm decided" ends the conversation badly.

| factor | why it is there |
|---|---|
| **severity** | a rank, not a number, so critical genuinely outranks two mediums |
| **confidence** | a 40 %-confidence fire is worth raising and is not worth the same as a certain one |
| **asset criticality** | a fire in the fuel store is not the event a fire in the car park is — and nothing upstream knows that, because the events engine sees a detection, not a site plan |
| **recency** | decayed, not cut off: an unacknowledged critical from this morning belongs *below* the fresh ones, not gone |

### The ordering flaw the shipped weights produced

Logarithmic damping on repeats was not enough. Measured:

```
19.7  medium speeding × 50   ← a stuck detector
15.1  high intrusion, warehouse, fresh   ← a real event
```

A chattering sensor outranking a real intrusion is precisely what teaches operators to stop reading an inbox.
Fifty repeats of a medium is a pattern worth noticing; it is not more urgent than something genuinely more
serious, and a human triaging by hand would never make that swap. **Repetition may now lift an alert toward
the next severity class and never past it** — severity remains the primary sort and repetition ranks within
it. After the cap: 9.6 against 15.1.

## Folding, and the trap inside it

The group key is type + location + entity, and getting it wrong fails in *both* directions:

- too coarse → two genuinely different fires fold into one alert an operator resolves once;
- too fine → a chattering detector produces a hundred rows.

Location before entity, deliberately: a fire is about a **place**, and folding on the entity would give one
alert per truck near the same fire. Events that genuinely are about one thing — speeding, dwell, a fall —
fold per entity, because two trucks speeding are two problems.

When a repeat folds in, the count and score rise but **the original timestamp does not**. An alert that keeps
resetting its own age can never escalate: it would look permanently fresh while nobody attends to it, which
is exactly the failure escalation exists to catch. A repeat that is *more severe* than the original raises the
alert's severity, so an incident that escalates in reality escalates in the inbox.

## Escalation is about the response, not the event

An unacknowledged critical is a **process** failure, not a more severe event. Conflating them would let a
rising score substitute for somebody actually looking at it — so escalation is a separate decision with its
own timer (2 min for critical, 10 for high), it fires once, and **acknowledging stops it**.

Mediums do not escalate on a timer. Escalating everything is the same as escalating nothing.

## Only medium and above reach the inbox

An inbox containing every zone entry is an inbox nobody opens, and then the criticals in it are invisible
too. Raising the floor is the single most effective thing that can be done for an alerting system's
usefulness.

Playbook progress is also excluded: it is the *response* to an alert, not an alert. Including it would double
every incident — one row for the fire, five for the response.

## What the inbox carries

The originating event's explanation is **carried through**, not replaced. The events engine knew why it fired;
an alert that discards that reasoning makes the operator go and find it. Decisions produced in response are
linked as they arrive, so the inbox shows what is being *done* about each item.

`GET /alerts/stats/summary` reports **mean time to acknowledge**, which is the figure that matters. A queue of
open alerts is normal; a *rising* time to acknowledge means the inbox is being ignored, and that is the
failure that makes alerting worthless.

The webhook is a convenience, not the record. Failures are counted and surfaced in `/health`, never retried
in-line — a webhook that blocks alert processing turns somebody else's outage into ours, and the alert is
already durable in Postgres.

## Endpoints

| | |
|---|---|
| `GET /alerts?state=&grouped=` | the inbox, escalated first then by score; grouped by default |
| `GET /alerts/{id}` | one alert with its full explanation |
| `POST /alerts/{id}/ack` | acknowledge and assign — stops the escalation timer |
| `POST /alerts/{id}/resolve` | close it, with a note |
| `POST /alerts/{id}/escalate` | escalate by hand |
| `GET /alerts/stats/summary` | inbox health, including mean time to acknowledge |
