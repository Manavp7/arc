# decision (M12)

Recommendations with ranked options, computed by OR-Tools, gated on a human.

```
events ──► decision ──► decisions (ranked options, rationale, approval=pending)
                    └─► postgres
```

## Nothing here executes anything

A decision is a **proposal** with `approval=pending`, and the only path to action is a human calling
`POST /decisions/{id}/approve`. That is enforced structurally rather than by convention: this service has no
actuator and no workflow client, so there is no code path from "recommended" to "done" for it to take by
accident.

Approval records **which option** was chosen, because an operator may prefer the runner-up — and a gate that
only accepted "yes" to the top option would make the ranked list decorative. When they do override the
recommendation it is logged and noted in the explanation, because *a human disagreeing with the optimiser is
the most interesting signal this service produces*: it is how the objective gets improved. A rejection keeps
its reason for the same reason.

## Ranked options, not an answer

The acceptance criterion is ranked options, expected effect, and a rationale — and that rules out returning
the optimum. **An objective function encodes somebody's opinion about trade-offs**, and presenting only the
winner hides both the runner-up and the fact that a choice was made.

Options come from solving the *same problem under different objectives*:

| strategy | how | why it can differ |
|---|---|---|
| **Balanced** | weighs suitability against travel time | the default |
| **Fastest response** | suitability flattened, so only travel separates candidates | a close but partly-suited responder |
| **Best suited** | speeds equalised, so only fit separates candidates | a distant but perfect responder |
| **Do nothing** | always offered, always costed | sometimes correct |

They are **real solves**, not perturbations of the winner. Inventing plausible runners-up would produce
numbers that correspond to nothing — worse than showing one option, because the numbers would look
checkable.

A measured example. Given a patrol 7 m from a fire and a drone 879 m away:

```
score 15.0  cost 0.88 km   Balanced + Best suited: Drone 0018 reaches inc-fire in ~59s (suitability 100%)
score  9.6  cost 0.01 km   Fastest response: Patrol A reaches inc-fire in ~1s (suitability 60%)
score -16.0 cost 0.00 km   Nothing is dispatched. 1 incident remains unattended (critical).
```

That is the trade-off an operator should be making, and it is invisible in a single recommendation.

**Metrics are recomputed from the real inputs.** The "best suited" solve equalises speeds to change what the
objective optimises; if its ETAs were reported as solved, the option would claim a 3-second response from a
responder that will actually take four minutes. A test asserts this specifically, because it is the kind of
error that produces a number that looks checkable and is false.

**When strategies agree, they are merged and say so.** Measured: with three responders and two incidents all
three chose the same plan and the list showed it three times. That is noise, and it buries something useful —
*when independent objectives agree, the recommendation is stronger than any one of them.*

## Three problems, three solvers

They are separate because their constraints do not compose: a routing objective measures distance, an
assignment objective measures suitability, a schedule measures lateness. Forcing them into one model would
need weights nobody could defend.

- **assignment** (CP-SAT) — a matching problem with capacities. Severity is a **rank**, not a score, because
  treating severities as evenly spaced would make two mediums outrank one critical. Leaving an incident
  unassigned is *allowed* and reported: a model forced to assign everything will send a low-battery drone
  across the site to a minor event.
- **routing** (VRP) — sequencing, minimising travel. Closes the loop by default, because a patrol that ends
  at the far corner has not finished, it has stopped.
- **scheduling** (CP-SAT intervals) — packing over time, minimising **priority-weighted waiting**, not
  makespan. Makespan is the intuitive objective and the wrong one: it optimises for the last truck leaving,
  which a scheduler achieves equally well by making one truck wait the whole session.

Every solve is time-bounded. An optimiser given an unbounded budget will take it, and a recommendation that
arrives after the incident is over is not a recommendation.

Both **worst-case** figures are reported alongside the totals — worst ETA, worst wait — because a plan that
reaches four incidents in a minute and the fifth in an hour is not a good plan, and an average hides exactly
that.

## The rationale explains; it cannot decide

An LLM is asked to explain the ranking in prose, given the numbers and told explicitly not to change the
order. A model that could reorder the options would make the optimiser decorative, and the optimiser is the
part that can be checked.

The template fallback is **not** a degraded mode. It is assembled from the same measurements the options were
scored on and is arguably more trustworthy than a generated paragraph, so it is labelled rather than
apologised for.

## Endpoints

| | |
|---|---|
| `GET /decisions?approval=` | recent recommendations with options and rationale |
| `GET /decisions/{id}` | one decision in full |
| `POST /decisions/{id}/approve` | the only path from proposal to action; takes an `option_id` |
| `POST /decisions/{id}/reject` | with a reason, which is the valuable part |
| `POST /decisions/recommend?kind=&zone_id=` | produce one on demand (the demo path) |
| `POST /decisions/schedule/docks` | schedule the waiting trucks onto the available docks |
