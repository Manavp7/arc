# missions (M17)

Mission control: objectives that complete themselves, assignment that cannot double-book, an append-only comms
log, derived progress, and a replay window that lines up with the timeline.

```
draft ──► active ──► completed
  │        ▲  │  └──► aborted
  │        │  ▼
  └────►   paused
```

## A mission is the one object a human owns

Everything else in this platform is machine-first: events are detected, alerts are ranked, decisions are
recommended, and a human approves. A mission is the opposite — a person declares an intent and the platform helps
them track it.

That shapes the service. It refuses *illegal* moves but not *unusual* ones, it explains rather than blocks, and
every automatic act it performs writes a line in the log — because a mission log with gaps in it is not evidence.

## Objectives complete themselves

This is what makes mission control belong in a spatial intelligence platform rather than in a task tracker. A
mission whose objectives are ticked by hand is a to-do list with a `zone_id` column. The platform already knows
whether a drone reached the fuel store, and asking a commander to tell it what it can see is both insulting and
unreliable — under load, the box that gets ticked is the one somebody remembers.

An objective with a `zone_id` completes when an **assigned** resource is observed there:

```
Objective met: Get eyes on the fuel store — observed drone-7 in fuel_store
```

Three properties, each load-bearing:

**Only assigned resources count.** A forklift wandering through does not satisfy "get eyes on the fuel store" —
the objective is about the mission's own resources doing the thing. Without this, a busy yard completes objectives
by accident, which is worse than not completing them, because it looks like success.

**Completion is sticky.** Once satisfied it stays done when the drone leaves. The requirement was "get eyes on
it", not "keep eyes on it for ever", and a progress bar that goes backwards is one nobody trusts twice.

**An objective with no zone stays manual.** "Confirm the area is safe" is a human judgement and the platform
cannot verify it. Being explicit about which objectives it can and cannot check is more useful than pretending
uniformity.

Occupancy comes from the world model within a **two-minute window**, because this platform deletes nothing: an
entity's `zone_id` is the last zone it was *seen* in, so without the window an objective could be satisfied by a
truck that has since driven to another county.

Re-evaluated on a tick as well as on events, because a resource can arrive in a zone without an event firing —
zone entry is debounced by hysteresis, which exists to stop an entity on a boundary producing a stream of
enter/exit pairs. An objective should not inherit a threshold designed for a different problem.

## Progress is computed, never stored

A stored percentage drifts from the objectives it summarises the moment one is added, and then two numbers on one
screen disagree about the same mission.

It also travels with a sentence, because `60%` prompts "which 40%?" every single time:

```
1 of 3 met; waiting on Check dock 3, Confirm the area is safe
0 of 3 met; waiting on … 2 objective(s) have no assigned resource that could satisfy them
```

That last clause is separate from `outstanding` on purpose: an outstanding objective is work in progress, an
unreachable one is work **nobody is doing**. A progress bar cannot distinguish them and a commander needs to.

A mission with no objectives reads **0%**, not 100%. "Nothing required, therefore complete" is technically
defensible and operationally awful — a mission somebody has not finished writing would show as finished.

## Assignment cannot double-book

Enforced by a partial unique index, not by a service check:

```sql
CREATE UNIQUE INDEX mission_resources_one_mission_idx
    ON mission_resources (tenant_id, resource_id) WHERE released_ts IS NULL;
```

Dispatching the same drone to two fires is exactly the failure that slips through a read-then-write check under
concurrency: two requests both see "not assigned", both write. The index makes the second write fail whatever the
service believed — the difference between an invariant and a habit. The service still checks first, so the common
case gets a useful message naming the mission that holds it; the index catches the race.

`mission_resources` is a table rather than the `resources` text[] on `missions` because the array cannot answer
"is this drone already committed?" without unnesting every active mission. The array remains as the denormalised
read path, since the console renders a mission whole.

**A paused mission keeps its resources.** That is what pause is for — the drone is still yours while you work out
what to do next. If pause released them it would be abort with extra steps.

## The comms log is testimony

Append-only, enforced by a Postgres trigger:

```
ERROR: UPDATE on public.mission_comms is forbidden: this table is append-only (SIO governance)
```

The distinction from `webhook_deliveries`, which is deliberately mutable, is whether a row records the world or
our own machinery. A delivery attempt is machinery and updating it is honest bookkeeping. A comms entry is
testimony — somebody said a thing at a time — and testimony that can be edited afterwards is worth nothing in the
review that follows a bad outcome.

Read **ascending**, unlike an alert list: a comms log is a narrative and reads forwards.

The platform writes to it too — state changes, assignments, objectives completing, resources released — naming
the cause each time. `Objective met` without a cause is the sort of log line that makes an incident review harder
rather than easier.

Alerts in the mission's zone are appended; events are not. An alert is something a commander should be told
about, and a log that also carries every zone entry buries it.

## Refusals explain

`completed` and `aborted` are terminal. A completed mission that can be reopened is one whose `completed_ts`
means nothing, and "was this finished at 14:20?" stops having an answer.

`paused → draft` is absent: un-starting a mission that has committed resources and written comms would make
`started_ts` a lie, and the log would describe events that, per the state, had not happened.

Each refusal names both the legal moves and *why*, because "cannot go from draft to completed" is a fact while
"a mission that never started cannot be complete — start it first, or abort it" tells the caller which of the two
things they meant.

## Completion is blocked, not forbidden

Open objectives block completion — and `force=true` overrides it. The platform does not get to tell a commander
that an operation is unfinished: sometimes an objective becomes irrelevant, and requiring somebody to tick a box
that is no longer true, to close a mission that is plainly over, teaches them to tick boxes.

What it *can* insist on is that the override is recorded:

```
Completed with objectives outstanding, by decision of cmdr: Check dock 3, Confirm the area is safe
```

A forced completion that looks identical to a clean one is how a review draws the wrong conclusion.

## Replay is one click

A mission already defines the two things a replay needs, so `GET /missions/{id}/replay` returns the window ready
for `POST /api/replay`. Making an operator copy timestamps into a separate form is the sort of gap that leaves a
feature technically present and never used.

A draft returns 409 rather than an empty window: a replay button that produces an empty stream is worse than no
button.

## Endpoints

| | | action |
|---|---|---|
| `GET /missions` | running first, then by recency | `mission.read` |
| `POST /missions` | create as `draft` | `mission.write` |
| `GET /missions/{id}` | with comms | `mission.read` |
| `POST /missions/{id}/state?to=` | lifecycle; `force=true` to override blockers | `mission.assign` |
| `POST /missions/{id}/resources?resource_id=` | commit a resource | `mission.assign` |
| `DELETE /missions/{id}/resources/{rid}` | hand it back | `mission.write` |
| `POST /missions/{id}/objectives` | add | `mission.write` |
| `POST /missions/{id}/objectives/{oid}` | tick by hand | `mission.write` |
| `POST /missions/{id}/comms` | append | `mission.write` |
| `GET /missions/{id}/replay` | the window | `mission.read` |

`mission.write` is wider than most write rules (operator, commander, admin) because a mission is the object a
human owns — an operator running an operation should not need a commander to write it down. `mission.assign` is
narrower (commander, admin) because assigning a drone decides where a physical thing goes and starting a mission
arms the objectives that will dispatch it.

Releasing deliberately falls to `mission.write`: handing a drone back is not the same kind of act as committing
one, and an operator who finishes with a resource should be able to free it.

## Health

Reports `degraded` when a resource is still held by a mission that is no longer running. Reported rather than
silently reclaimed — the drone is genuinely unavailable to everyone else, and quietly freeing it would hide a bug
in the release path.
