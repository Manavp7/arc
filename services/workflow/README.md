# workflow (M15)

Response playbooks: a high-severity event drives a sequence of steps, and every step is visible while it
happens.

```
events ──► workflow ──► events    (one WORKFLOW_STEP event per transition)
                    └─► postgres  (workflow_runs, upserted with per-step state)
```

Three playbooks ship: `FireResponsePlaybook` (dispatch drone → notify security → close gate → open
incident → generate report), `IntrusionPlaybook`, and `DwellEscalationPlaybook`.

## Compensation is the part that matters, and the part usually missing

A five-step fire response that fails at step four has already dispatched a drone and closed a gate.
**"Failed" is not a state anyone can act on:** the gate is still shut, the drone is still airborne, and
nothing says why. So every step that changes something declares how to undo it, and a failed run reverses
what it did.

**In reverse order**, because the dependencies run that way: a gate closed *after* a drone was dispatched
must be reopened *before* the drone is recalled, or there is a window where the site is sealed and nothing
is watching it.

Three consequences worth stating:

- **Some steps cannot be undone.** Once security has been notified, un-notifying them is not a thing.
  Writing `compensate=None` would silently do nothing, which reads as success — so the absence of an undo
  is explicit (`irreversible=True`) and is recorded in the run.
- **A failed compensation escalates.** That is the worst case: a gate closed by a response that then rolled
  back, with no automated path left to open it. The world is in a state nobody chose, so it degrades
  `/health` rather than becoming a log line nobody reads.
- **The failed step is not compensated.** Undoing something that did not happen is at best a no-op and at
  worst a second action.

## Retries need idempotency, not just a count

A step that times out **may already have acted** — a timeout is what a slow network looks like, so the
retry is the expected path, not the exceptional one. Retrying without idempotency double-dispatches the
drone.

Every activity is idempotent on a key derived from the run and step, enforced by a decorator rather than by
asking each activity to remember. Idempotency is deliberately **per run**: two fires should dispatch two
drones.

Timeouts are per step, backoff is exponential (a step failing because a service is restarting wants a
second attempt *later*; three immediate retries are one retry with extra logs), and a **missing activity
fails immediately** — a definition error is not transient, and retrying it three times reaches the same
conclusion slower.

## An optional step's failure does not roll back the response

A report that will not render must not undo a drone dispatch and a gate closure. But it is recorded as
**failed**, not softened into a "skipped" state that would hide a real failure behind a word that sounds
deliberate. What differs is the *run's* outcome, not the step's.

## Progress is emitted per step

One event per transition, because a five-step response that reports only "running" and then "completed" is
indistinguishable from a hang *while it is happening* — which is exactly when someone is watching.

A broken progress hook cannot fail a run. Progress is observability; a UI subscriber that raises must not
roll back a fire response, which is what an unguarded `await` would cause.

## Cooldowns, again

`fire_detected` trips on nearly every frame while a fire burns. Without a per-subject gap, one fire starts a
hundred playbooks, each dispatching the same drone — the same lesson as the events engine's cooldowns,
reappearing one layer up.

## Dry run is the default

`SIO_WORKFLOW_DRY_RUN=true` unless set otherwise. A workflow engine that can only be exercised by actually
closing a gate is one nobody exercises, and the demo needs to run a five-step response without
consequences. Every dry-run step records what it **would** have done, so the run log is still an honest
account.

Where no actuator exists at all — there is no drone command interface in this build — the activity says so
rather than reporting a dispatch that did not happen. **The run record is what an incident review reads**,
and a record that flatters the system is worse than a gap in it.

## Why the inline runner, and where Temporal fits

`InlineRunner` is not a stub. It is the runner CI and the demo use, and it implements the semantics that
matter: per-step timeouts, bounded retries with backoff, compensation in reverse, and progress per step.

What Temporal adds is durability across a process death — real, and the reason the `Runner` port exists.
What it does not add is any of the above, and burying those semantics inside a framework would make them
untestable without one. `temporalio` is an optional dependency for that reason.

## Endpoints

| | |
|---|---|
| `GET /workflow/playbooks` | every playbook, its triggers, its steps and their compensations |
| `GET /workflow/runs` | recent runs with per-step status — what the UI renders |
| `GET /workflow/runs/{run_id}` | one run in full, from memory or the database |
| `POST /workflow/run/{playbook}?zone_id=` | start one by hand; the run records that a human did |
