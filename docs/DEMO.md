# Giving the demo

Five minutes, on macOS, from a clean clone. Written to be read aloud.

---

## Before the room (15 minutes, once)

```bash
git clone <repo> sio && cd sio
just setup            # ~8 min: brew deps, uv sync, npm install, model download
just services         # ~30 s: postgres, redis, neo4j, minio
just doctor           # says what is missing, if anything
just dev              # ~90 s: the platform, 19 processes
just seed             # the yard: 17 zones, cameras, sensors
```

Then, in another terminal:

```bash
just demo
```

**Do this once before the room, not in it.** The first `just demo` warms the language model — Ollama loads
two gigabytes of weights on the first question, so a cold copilot answers in ~17 s and a warm one in ~7 s.
That difference is the whole impression the copilot makes.

Leave everything running. Open <http://localhost:5173>.

---

## The five minutes

### 0:00 — "This is a logistics yard, live"

Point at the map. Say what is on it: zone outlines, and dots that are moving.

> Every dot is an entity the platform has *decided* exists. Nothing here is a raw sensor feed — cameras,
> GPS trackers and IoT sensors are being fused into one belief about each physical thing, and the platform
> is doing it now, at about thirty observations a second.

Click a truck. The panel shows **time on site** and, under `sources`, **which sensors** produced the belief.

> That last part is the point. Two different sensor types agree this is one truck. If they disagreed, the
> confidence would say so.

### 0:45 — "Something is wrong" (the ALERTS tab)

Click **alerts**.

> This is the inbox an operator works from. It is ordered, and the ordering is the whole product: the
> number on the left is a priority, and the line under the title is *why it scores that* — severity,
> how confident the detector was, how critical the location is, how many times it has happened.

Point at a folded alert (`×N occurrences`).

> Repeats fold into one row rather than filling the inbox. A chattering sensor cannot bury a real fire —
> and it cannot outrank one either, because repetition is capped so it can never promote an alert past a
> genuinely more serious one.

Point at an escalated row.

> Amber line: this one has been sitting unacknowledged past its timer. That is a *process* failure, not
> the event getting worse, which is why it is a separate sentence from the priority.

Click **ack** on it.

> Acknowledging stops the escalation clock. Somebody now owns it.

### 1:30 — "Why should I believe you?" (the drawer)

Click an alert's **body** (not its buttons). The drawer opens.

> Every conclusion in this platform can explain itself, and they all explain themselves the same way —
> events, alerts and recommendations open this identical drawer.

Walk down it:

- **confidence** at the top, because it changes how you read everything below;
- **why** — the reasoning, in order;
- **evidence** — the actual observation and frame ids, in full, because their purpose is to be looked up;
- **considered and not chosen** — what it ruled out, and why.

> If anything here had been produced with something missing — no model, a gap in the data — there would be
> a banner at the top saying so. An answer that arrived by fallback does not get to look like one that
> did not.

### 2:15 — "What are you going to do about it?" (the DECISIONS tab)

Click **decisions**, then **show N options** on a pending card.

> The platform has worked out what it would do, ranked the options with a constraint solver, and **not
> done any of it**. Every option is here, including the ones it did not pick, with the cost and the risk —
> and the infeasible ones with the reason, because "why isn't the nearer vehicle being sent" is a question
> with an answer.

> Approving is a human action. Until somebody presses this, nothing is dispatched. That is not a policy
> setting, it is how the code is arranged: the service that recommends cannot execute, and the agent that
> executes only accepts an *approved* decision — and only a recent one, so a stale approval cannot be
> replayed into an action.

Approve one.

### 3:00 — "And when it does act" (the MISSIONS tab)

Click **missions**.

> A critical event triggers a playbook automatically. Each step has its own status rather than a progress
> bar, because a bar says sixty per cent and hides which step is stuck. Retries are counted. And if a step
> fails, the completed steps are *undone* in reverse order — you will see those marked with a return
> arrow, which means the run failed and cleaned up after itself.

### 3:30 — "What happens next" (the FORECAST tab)

Click **forecast**.

> The shaded band is the interval, drawn first and more prominently than the line, because a forecast
> drawn as a single line is a lie told with a chart. The intervals are measured against held-out history,
> not assumed.

Find one that says *"effectively the whole range"*.

> And where the interval is too wide to be useful, it says so instead of pretending. That is the
> difference between a dashboard and an instrument.

### 4:00 — "Just ask it" (the COPILOT tab)

Click **copilot**, then a suggestion. **Wait** — a local 3-billion-parameter model takes a few seconds.

> This is a 3 B model running on this laptop. No API key, no data leaving the machine.

When the answer arrives, click **how?**.

> Every tool call it made, in order, with timings. When an answer looks wrong this is where you find out
> whether a query returned nothing or the model invented a figure. A copilot that cannot show its work is
> a liability, not a feature.

### 4:30 — "And it never forgets" (the timeline)

Drag the scrubber at the bottom backwards.

> The map is reconstructing the world as it was at that instant. Nothing in this platform is ever
> overwritten — every entity and every relationship is stored with the window of time it was true for, so
> any past moment can be rebuilt exactly. This is what makes the audit trail worth having.

---

## The next five, if they are still asking

Everything above is the core loop. These four are what to reach for when somebody wants to know what else is
there — one each, not all four, and let them pick.

### Mission control (the **missions** tab)

Create a mission, give it an objective with a zone, commit a resource to it.

> A mission is the one object here that a *person* owns. Everything else is machine-first — events are
> detected, alerts ranked, decisions recommended. This is a human declaring an intent.

Then wait. The objective ticks itself.

> That is the part worth watching. An objective with a zone completes when an **assigned** resource is
> observed there — the platform already knows whether the drone arrived, so asking a commander to tell it
> what it can see is both insulting and unreliable. A forklift wandering through does not count; only what
> you committed.

If they ask about governance, try to commit the same drone to a second mission. It is refused, by a database
constraint rather than a service check — "dispatching the same drone to two fires is the failure that slips
through a read-then-write under concurrency".

### Analytics (the **analytics** tab)

> The dwell distribution is the one to look at. It reports a *shape*, not just a mean: "long right tail, p95
> is 24x the median, so the mean of 2.4 describes almost nobody". A single average across a bimodal
> distribution is a number that is true and useless.

### The 3D twin (the **twin** tab)

Toggle **camera coverage**.

> A 3D view of a flat yard is mostly a worse 2D map. The one thing it genuinely adds is this: a camera's
> field of view is a **volume**, and the map can only draw its shadow. Two cameras that overlap at head
> height but not at ground level look identical on the map and obviously different here.

Say the caveat before they spot it: the mast height is an assumption, and the caption on screen says so.

Worth mentioning if the audience is technical: the tab is lazy-loaded. Cesium is 4.5 MB and arrives when you
open it, so the operators who never do pay nothing.

### The no-code builder (the **builder** tab)

Type a workflow with a deliberate mistake in it — an activity that does not exist.

> It refuses, and it names the eight that do. Every problem at once, with the fix — not the first one, then
> the next one, then the next. The workflow it writes is executed by the *same* engine as the Python
> playbooks, with the same retries, compensation and cooldowns.

---

## Authentication, if they ask "is any of this secured?"

Do not skip past this; it is a short and good answer.

> Every request carries a signed token, every route maps to a policy action, and every denial is written to
> an append-only audit table that Postgres itself refuses to let anyone update. The demo runs with a local
> issuer so there is no login screen in the way — Keycloak and OPA drop in behind the same interfaces, and
> the wiring is documented.

If they push: `curl` any endpoint without a token in front of them. The refusal names the action and the
role required, rather than saying 403.

---

## What to say if it is slow

Be straightforward about it. The honest version lands better than an apology.

| what happens | what to say |
|---|---|
| the copilot takes 15-20 s | "That is a cold model — it is loading two gigabytes of weights. The next one will be about seven seconds." Then ask a second question and let them see it. |
| the map stutters | "This is software rendering on a VM with no GPU. On the target hardware deck.gl runs on the GPU." (On your Mac it will be fine — this is a VM caveat.) |
| a panel is empty | Say which panel and why: forecasts need a few minutes of history before the service will commit to a number, and it refuses rather than guessing. **That is a feature; say so.** |
| nothing has alerted yet | `just demo` again. The yard is a simulation; incidents happen when injected. |
| a service is down | `just doctor`. It names the service and the command to fix it. Do not improvise. |

**Do not** apologise for the model being small. Say what it buys: no API key, no data leaving the machine,
and a tool-selection accuracy measured at 95 % on this repository's own fixture — see `docs/MODELS.md` for
the numbers and the four models that lost.

---

## The use cases, if asked

| | what to show |
|---|---|
| **UC1** who is on site, and for how long | click an entity: time on site, and the sensors behind the belief |
| **UC2** something is wrong | the alerts inbox, and the drawer behind a row |
| **UC3** what happens next | the forecast tab, including one that admits its interval is useless |
| **UC4** ask in English | the copilot, then `how?` |
| **UC5** what did it look like at 14:32 | drag the timeline scrubber |
| **UC6** who decided what, and when | the decisions tab, plus `curl localhost:8000/api/audit` |

---

## Re-running back to back

```bash
just demo-reset && just demo
```

`demo-reset` resolves the alerts and rejects the pending decisions. It does **not** delete anything — this
platform is append-only by design, and a reset that truncated tables would demonstrate the opposite of the
product. The history stays; only the working state is cleared.

If it warns that something is still producing alerts, a fire is still burning in the simulation. Wait for it
to expire (15 min of simulated time) or just run the reset again.

---

## Known limitations, stated plainly

Say these before you are asked. They are all deliberate.

- **The yard is simulated.** Real connectors ship — RTSP cameras, MQTT sensors, MAVLink drones, Sentinel-2
  imagery, a SQL query against a WMS — and each is behind the same port as the simulator. The demo drives a
  physics simulation so the incident is reproducible on demand, which a real camera cannot be.
- **Playbook steps are dry-run by default.** They record what they *would* have done. A workflow engine that
  can only be exercised by actually closing a gate is one nobody exercises.
- **Fire detection is a colour-and-motion heuristic**, not a trained model. It runs on the real rendered
  frames — the detection is genuine, the detector is simple.
- **The copilot is a 3 B model.** It selects the right tool ~95 % of the time and gets arguments right ~81 %.
  Restraint (knowing not to call a tool for "hello") is handled in code rather than trusted to the model,
  because the best candidate still queried the database to answer a greeting one time in three.
- **Keycloak and OPA are optional.** The dev default is a signed local JWT and a permissive policy, both
  tested. Production wiring is documented, not demonstrated.
- **Single tenant in the demo.** Every table and query is tenant-scoped; the demo runs one.
- **The 3D twin's mast height is a guess.** The source table records a camera's position and its ground
  coverage, not how high it is mounted, so the frustum apex assumes 8 m. The caption on screen says so —
  somebody judging whether a camera clears a container stack needs to know which part is data.
- **The GPU profile boots but four of its adapters are stubs.** `SIO_PROFILE=gpu` flips every seam and the
  test suite passes under it; Kafka, Qdrant, DeepStream and TimesFM construct and then refuse, each naming
  what blocks it. Writing them untested would produce code that looks finished and has never run. The two
  that are real — an OpenAI-compatible LLM client and Memgraph — are tested.
- **Detection mAP on the demo fixture is 0.23**, and that number measures how well the model detects *sprite
  renderings*, not trucks. It is a regression detector for the pipeline, not a claim about accuracy. `just
  eval` prints it alongside HOTA, copilot accuracy and event precision/recall, each with a floor.
