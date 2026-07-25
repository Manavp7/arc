# agents (M14)

Two agents on their own schedules: observe → reason → decide → **propose**. Acting is a separate entry that
only a human can trigger.

```
                    ┌─► decisions (approval=pending)  ──► a human approves ──┐
agents (scheduled) ─┤                                                        │
                    └─► audit_log (append-only)      ◄── every step ◄────────┘
                                                                             │
                                                          workflow playbook ◄┘
```

## An agent may not act on its own proposal

That is the whole design, and "we check a flag" is not enforcement. Here it is structural:

`act` takes an **approved `Decision`** as its argument and has no other entry. It cannot construct one, it
cannot approve one, and the approval field it reads is written by a *different service* in response to an
HTTP call from a human. So the sequence is not "propose, then check whether we may act" — it is two separate
entries into the loop, one of which only a human can trigger. **A bug in the reasoning cannot produce an
action, because reasoning does not have the argument that acting requires.**

A test tries anyway, with an agent whose reasoning returns `summary="EXECUTE IMMEDIATELY"`. Nothing happens.

Three refusals, each tested:

- **Already executed** → refused. Approval events arrive twice under at-least-once delivery.
- **Approved but no option chosen** → refused, not guessed. Which option a human meant is precisely the
  judgement the gate exists to keep with them.
- **Approved action with no playbook** → reported as un-executed. Substituting an action a human did not
  approve is the one thing this design exists to prevent.

Proposals are filed by **HTTP to the decision service**, not by writing the row. A service that can insert
its own approved decision is one approval-field bug away from acting without a human.

Execution goes through the **workflow service**, so an approved decision runs a playbook with retries and
compensation rather than reaching for an actuator. One implementation of "how to do a thing safely".

## The agents look for what nothing else is watching

There is no point in an agent that re-raises what the rule engine already fires. The events engine notices a
fire; an agent that also notices the fire has added a second voice saying the same thing. What an agent adds
is **patterns over time**, which a stateless rule cannot see:

- **security** — a *sensitive zone that is occupied but has produced no events*. An empty event stream from a
  fuel store is either a quiet fuel store or a blind one, and the difference is worth asking a human about.
- **logistics** — the *queue*. `dwell_exceeded` fires when one truck has been too long; this looks at several
  trucks waiting while docks sit free, which no per-entity rule can see because no single truck is
  misbehaving.

Both need **two conditions**, not one: trucks waiting while every dock is busy is a capacity problem an agent
cannot fix, and proposing a schedule for it would be theatre.

Reasoning is a short chain of stated conditions over observed facts, and every proposal carries the numbers
it was based on. **An agent whose reasoning is inscrutable cannot be trusted with an approval gate**, because
a human asked to approve something must be able to see why it was proposed, and "the model said so" is not a
reason anyone can act on. A model is used for wording, never for the decision.

An agent that **cannot see** says so rather than concluding all is well. Silence from a broken sensor and
silence from a quiet site look identical from here, and only one of them is fine.

## Memory that changes the next decision

The "learn" step is usually the one that does nothing — a loop that records its own actions and never reads
them back has a diary, not a memory. Here it is retrieval that alters the proposal:

- **The outcome is written when it is known**, not at proposal time. A memory written at proposal time records
  an *intention*, and counting an intention as a success is how an agent learns the wrong lesson. Entries
  start `pending` and are updated on the human's verdict.
- **The human's reason is the most valuable field.** A proposal of a kind that was rejected before is
  re-proposed *with the precedent attached* — the operator deserves the context, and repeating a rejected
  proposal silently is how automation earns distrust.
- **Similarity is not relevance.** A nearest-neighbour search always returns something; treating its best hit
  as a precedent regardless of distance is how an agent comes to "remember" an unrelated incident. A floor
  applies, and misses are reported as misses so "no precedent" is distinguishable from "nothing was close
  enough".
- **With no vector store, memory degrades to nothing**, not to a keyword match. A keyword "precedent" would be
  a different thing wearing the same name, and an agent citing it would be misleading about why it acted.

## Quiet cycles are recorded

"I looked and there was nothing" is a useful audit entry, and its absence makes a quiet agent
indistinguishable from a stopped one. Most cycles are uninteresting, and an agent that proposes something
every time it looks is one whose proposals get ignored.

The audit trail is the `audit_log` table — append-only, enforced by a trigger. That is the point: an agent's
trail is worth having precisely because nothing, including the agent, can edit it afterwards.

## Endpoints

| | |
|---|---|
| `GET /agents` | the agents, their schedules, and the gate they cannot open |
| `GET /agents/cycles` | recent loop turns, including the ones that proposed nothing |
| `POST /agents/{name}/run` | run one loop now — still only proposes |
| `GET /agents/audit` | the trail: observations, proposals, verdicts, executions |
| `GET /agents/memory?situation=` | what memory would recall — the "learn" step, made inspectable |
