# events (M9, M22)

Turns facts into events, and owns the event log.

```
entities   ──┐
events     ──┼──► events ──► events   (rule-derived events + anomalies)
detections ──┤              └► postgres (the append-only event log)
raw.iot    ──┘
```

## Adding a rule requires no code change

That is the M22 acceptance criterion, and it rules out the tidy design where each rule is a Python
class — because then adding one means editing, testing and deploying a service. So a rule is a YAML
document in `infra/rules/`, and the engine is its interpreter.

A test asserts this literally: it writes a rule that did not exist when the service was written
(`purple_forklift_in_the_office`) to a temporary directory and asserts it fires. No Python, no class, no
import.

Pure data cannot express everything, and pretending otherwise grows a language until it is a bad
programming language. The line drawn here:

- **YAML declares the rule** — what to match, over what window, how severe, how often it may fire.
- **A small set of primitives evaluates it** — comparison operators and four aggregates.

Adding a *rule* is data. Adding a genuinely new *kind of condition* is a new primitive, which is a code
change — and that is the honest boundary, because a new condition kind is new semantics, not new
configuration.

## Facts: one shape for everything

Rules can only be written against one shape, so every message is normalised into a `Fact` with
dotted-path fields. The flattening is explicit rather than a generic recursive walk of the payload: a
generic walk would expose every internal field to rule authors, which sounds flexible and is a trap —
the rules become coupled to the wire format, and renaming a field inside a service breaks a YAML file
nobody remembers writing. What `facts.py` exposes is a **contract**, small enough to read.

Two details that exist because of how humans write rules:

- **Speed is exposed in m/s *and* km/h.** A yard speed limit is posted in km/h, and making every author
  divide by 3.6 in YAML is how a rule ends up wrong by a factor of 3.6.
- **Comparisons coerce.** `severity: high` is a string, `value: 20` is an int, and a field holding
  `"20"` means the same thing to the person who wrote the rule. An engine that disagrees will be called
  broken, correctly.

## Three rule shapes, because the required list needs all three

| shape | fires on | rules |
|---|---|---|
| **match** | each fact as it arrives | `fire_detected`, `zone_breach`, `forced_door`, `dwell_exceeded` … |
| **window** | an aggregate over recent facts per subject | `speeding`, `crowd_gathering`, `congestion`, `suspicious_meeting` |
| **absence** | a subject that *stopped* reporting | `machine_stopped` |

**Absence is the shape people forget**, and it is where the interesting failures live. An engine that
only reacts to messages it receives can never notice a machine that stopped — or a camera that went
dark. It fires only for subjects that established a baseline first (`requires_history`), because
otherwise every machine on site is "stopped" the moment the engine starts, which is how an absence rule
gets muted permanently and never switched back on.

## The details that decide whether this is usable

**Cooldowns are not optional.** A condition true of a parked truck is true of every message about it.
Without a per-subject cooldown, one stationary vehicle in a restricted zone emits an event per second
until someone deletes the rule. A test drives 120 messages through a 60-second cooldown and asserts
exactly two events.

**Windows filter membership, aggregates test the group.** `when` decides which facts enter the window;
the aggregate decides whether the group is interesting. That split is what lets "five or more distinct
people in a zone within a minute" be a rule rather than a special case.

**`count_distinct`, not `count`, for crowds.** Counting messages would let one person standing still
under a 4 Hz camera register as a crowd. Counting identities is the difference between a useful alert
and confident nonsense.

**Peak over a window, not the instantaneous value, for speeding.** One noisy fix can put a parked truck
at 40 km/h, and a rule that believes a single sample will cry wolf until it is switched off.

**One bad rule must not stop the others.** A YAML typo is routine; a loader that raises on the first
problem means one malformed file silently disables the fire rule. Errors are collected, surfaced in
`/health` and `/events/rules`, and everything else loads. Duplicate ids are rejected rather than
resolved by order, because "the last one wins" is a rule nobody can see when reading either file.

**Hot reload preserves window state.** Clearing the windows on reload would blind every window rule for
its whole span, so editing one rule would silently suppress crowd detection for a minute.

**Fire trips on the raw detection.** Not on a tracked or fused entity: the latency of the full pipeline
is unacceptable for fire, so it is better to raise it now at lower confidence than three seconds later
with a track id attached. The explanation says so, so an operator knows to confirm against the frame.

**Composition over duplication.** `zone_breach` is a rule *about the spatial service's event*, narrowed
by entity type. Re-deriving geometry here would create a second point-in-polygon implementation, and two
implementations eventually disagree.

## Anomalies (UC6)

Rules cover what someone thought of. UC6 needs the opposite: notice that something is *odd* without
having been told what odd looks like — and say **which measurements** were odd, because "anomaly score
0.87" is not something an operator can act on.

The default detector is a **per-feature robust z-score** over windowed rates, not PyOD's
IsolationForest, and the trade is worth stating plainly. On the handful of rates this service produces —
entities, detections and events per minute, mean speed, person fraction — a forest is not measurably
better, and it is considerably worse at the part that matters: a forest gives a score, and attributing
that score to features needs SHAP or a permutation study. A per-feature z-score is *inherently*
attributable, because the per-feature deviation is the output rather than a reconstruction.

`PyODDetector` is available behind the same interface (`SIO_ANOMALY_DETECTOR=pyod`) for when the feature
space grows enough that interaction effects matter — a forest can catch "throughput normal AND occupancy
normal BUT that combination never happens", which no per-feature test can. It attributes by
leave-one-feature-out perturbation, at O(features) model calls per alert.

**Median and MAD, not mean and standard deviation.** Both of the latter are dragged toward an outlier
that is *inside* the window, which lets a large anomaly hide itself. That failure is called masking and
it is precisely what this detector exists to catch, so using statistics with that weakness would be
self-defeating. A test puts a 500× spike into the history and asserts a later, smaller anomaly is still
caught.

**Rates, not totals.** A longer sampling interval must not look like a busier site.

## Endpoints

| | |
|---|---|
| `GET /events/rules` | every rule, plus the ones that failed to load and why |
| `POST /events/rules/reload` | reload now instead of waiting for the timer |
| `GET /events/engine` | windows, firing counts, cooldown suppressions, detector state |
| `GET /events/recent` | recent events with their explanations |
| `POST /events/simulate` | evaluate a hand-written fact without publishing — the tool a rule author actually needs |
