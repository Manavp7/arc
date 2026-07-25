# simulation (M11)

What-if projections, seeded from the live world model, that change nothing.

```
api /entities + /spatial/zones ──► WorldSnapshot (frozen) ──► Scenario.project() ──► SimulationRun
                                                                                 └─► simulation topic
                                                                                 └─► postgres
```

## A what-if must be counterfactual

The platform already had a tool called `run_simulation` that **injected a fire into the live simulated site**.
That is the opposite of a projection: asking "what if a gate closed" and having a gate close is the difference
between a forecast and an accident.

So a scenario receives a frozen `WorldSnapshot` — an immutable, plain-data copy read once at the start of the
run — and **nothing else**. No client, no pool, no bus. It cannot reach anything live because it is never
handed anything that could, which is a property of the types rather than of the discipline of whoever writes
the next scenario. `tests/unit/test_simulation.py` asserts the signature.

Every run records `seeded_from_ts`. A projection is only meaningful relative to a state of the world, and one
that cannot say which state it started from cannot be checked afterwards — which is the only way anybody finds
out whether these numbers are worth anything.

## On SimPy and Mesa

The PRD names both. Neither is used, and the reasoning is the one that kept PyTorch out of the perception
stack.

SimPy's value is managing a simulation clock across many interacting processes. The queueing scenarios here — a
dock going down, a gate closing — are single-server queues over a handful of trucks, where the projection is
closed-form arithmetic plus a short deterministic loop. Wrapping that in a generator-based event loop would add
a dependency, obscure the arithmetic, and make the numbers harder to check by hand. Mesa is heavier still, for
scenarios with at most a few dozen agents and no emergent behaviour worth discovering.

`fire_spread` is the one scenario with genuinely spatial dynamics, and the PRD's own description names the
right tool: *"cellular spread over site grid + wind"*. That is a cellular model — thirty lines, no framework.

A test asserts neither module is imported, so the decision is recorded as a check rather than only as a
comment.

## The scenarios

| scenario | question | quantifies |
|---|---|---|
| `gate_closure` | if a gate closes, what queues? | throughput lost, mean extra wait, trucks redirected |
| `dock_breakdown` | how long does the queue get? | capacity before/after, backlog, drain time |
| `fire_spread` | where does it reach, and who is in the way? | downwind reach, entities and **people** at risk, zones reached, per-entity arrival times |
| `flood_level` | what is under water at this level? | zones flooded, entities affected, ground units stranded |
| `drone_battery_death` | which drones cannot get home? | drones at risk, and how far short each one is |
| `bridge_collapse` | what is cut off? | zones isolated, ground entities stranded |

Each returns numbers **and a named list of affected entities**, which is the plan's acceptance criterion. A
projection that returns a paragraph has not run.

### Edge cases a naive model gets wrong

Closing the **only** gate does not halve throughput — it stops the site, and the projection says so with
*higher* confidence rather than lower. The same for the only dock. Both were worth building explicitly, because
"redistribute across the remaining gates" divides by zero when there are none.

Every return path is quantified, including the trivial ones. My own acceptance test caught the no-drones branch
returning an empty `kpi_deltas`: a projection with no numbers has not run, even when the honest answer is zero.
"0 of 0 drones at risk" tells a reader the question was asked and answered; an empty dict looks identical to a
crash.

A refusal — an unknown zone — is quantified too, **and names the zones that do exist**, because the caller is
often a language model and a bare refusal leaves it to guess again.

## Every projection states its assumptions

Not decoration. Every constant in these projections was **chosen rather than measured**, and a number shown
without its assumptions invites an operator to treat a guess as a measurement. They are rendered into the
explanation, so they travel with the answer:

```
assumes: fire spreads at 0.05 m/s across open yard, which is an estimate and not a measurement
assumes: the site is treated as uniformly combustible; surfaces, firebreaks and suppression are not
         modelled, so this is a worst case
assumes: this site has no road graph, so reachability is approximated by straight-line paths
```

`flood_level` is the starkest: there is no terrain model, so every zone is treated as at datum. That makes the
projection a worst case, it says so in the summary as well as the assumptions, and one of its recommendations
is to go and survey the elevations.

**One constant has real provenance.** The drone drain rate is the yard simulator's own
`Drone.drain_per_minute`, because a projection about the simulated site should use the simulated site's
physics. I first wrote 1.2 with a comment claiming it came from the simulator; it did not, the simulator uses
1.6. A test now reads the simulator's default and asserts they agree, which is what makes the provenance claim
true rather than aspirational.

## Endpoints

| | |
|---|---|
| `GET /simulations/scenarios` | the scenarios and their JSON Schema parameters — one source for the API, the UI and the copilot tool |
| `POST /simulations` | run one; returns the full `SimulationRun` with explanation |
| `GET /simulations` | recent runs |
| `GET /simulations/{run_id}` | one run |
| `GET /simulations/world/snapshot` | what a projection would be seeded from right now |

The snapshot endpoint exists because "why did the simulation say that?" is usually answered by looking at what
it was given, and an operator cannot inspect a frozen copy that only ever existed inside one request.

Results are published to the `simulation` topic so the decision engine can consume them. A projection nobody
acts on is a chart.
