"""Alert prioritisation, deduplication and grouping (PRD M16).

An alerts inbox lives or dies on its ordering. Get it wrong and the inbox becomes a list nobody reads, at
which point the alerting is worse than none: it converts a real signal into noise that people have learned
to dismiss.

The score has four factors, and each is separated so the ranking can be *explained* rather than merely
produced. Every alert carries the sentence that justifies its position, because "why is this at the top"
is the first question an operator asks and "the algorithm decided" ends the conversation badly.

* **severity** — the event's own claim about how bad it is. A rank, not a number, so critical genuinely
  outranks two mediums.
* **confidence** — how sure the producer was. A 40 %-confidence fire is worth raising and is not worth
  the same as a certain one, and multiplying by confidence is the honest way to say so.
* **asset criticality** — where it happened. A fire in the fuel store is not the same event as a fire in
  the car park, and no amount of severity scoring inside the events engine knows that.
* **recency** — decayed with age, so a stale alert sinks. Deliberately *decay*, not a cut-off: an
  unacknowledged critical from an hour ago should fall below a fresh one without disappearing.

Escalation is separate from scoring on purpose: an unacknowledged critical is a *process* failure, not a
more severe event, and conflating them would let a rising score substitute for somebody looking at it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sio_core import get_logger
from sio_schemas import AlertState, Event, utc_now

log = get_logger("sio.alerts.scoring")

#: Severity as a rank. Evenly-spaced numbers would let two mediums outrank one critical.
SEVERITY_WEIGHT: dict[str, float] = {
    "info": 1.0,
    "low": 2.0,
    "medium": 5.0,
    "high": 12.0,
    "critical": 30.0,
}

#: How much the location matters. A fire in the fuel store is not the event a fire in the car park is, and
#: nothing upstream of here knows that — the events engine sees a detection, not a site plan.
ZONE_CRITICALITY: dict[str, float] = {
    "fuel_store": 2.0,
    "warehouse": 1.4,
    "office": 1.3,
    "gate_a": 1.2,
    "gate_b": 1.2,
    "apron": 1.1,
}
DEFAULT_CRITICALITY = 1.0

#: Age at which an alert's score has halved. Two hours: long enough that a shift handover still sees what
#: mattered, short enough that yesterday's alerts are not competing with this minute's.
RECENCY_HALF_LIFE_S = 7200.0

#: How long a critical alert may sit unacknowledged before it escalates.
ESCALATE_AFTER_S: dict[str, float] = {
    "critical": 120.0,
    "high": 600.0,
}

#: Window within which repeats of the same group fold together instead of arriving separately.
DEDUP_WINDOW_S = 900.0


def severity_weight(severity: Any) -> float:
    return SEVERITY_WEIGHT.get(str(severity), 3.0)


def zone_criticality(zone_id: str | None) -> float:
    return ZONE_CRITICALITY.get(zone_id or "", DEFAULT_CRITICALITY)


def recency_factor(last_ts: datetime, *, now: datetime | None = None) -> float:
    """Exponential decay on age, floored so an old alert sinks without vanishing.

    A floor rather than zero, because an unacknowledged critical from this morning still belongs in the
    inbox — below the fresh ones, but present. An alert that decays to nothing has been silently resolved
    by the passage of time, which is not a thing that happens.
    """
    age_s = max(0.0, ((now or utc_now()) - last_ts).total_seconds())
    return max(0.05, math.exp(-age_s * math.log(2) / RECENCY_HALF_LIFE_S))


@dataclass
class Scored:
    """A score with the reasoning that produced it."""

    score: float
    reason: str
    factors: dict[str, float] = field(default_factory=dict)


def score_alert(
    *,
    severity: Any,
    confidence: float,
    zone_id: str | None,
    last_ts: datetime,
    count: int = 1,
    now: datetime | None = None,
) -> Scored:
    """Score an alert and say why it scores that.

    Repeats raise the score **logarithmically**. Linear would let a chattering sensor outrank a fire: fifty
    repeats of a medium would beat one critical, and the fifty repeats are usually a stuck detector rather
    than fifty times the problem.
    """
    weight = severity_weight(severity)
    criticality = zone_criticality(zone_id)
    recency = recency_factor(last_ts, now=now)
    confidence = max(0.05, min(1.0, float(confidence)))
    repetition = _repetition_factor(count, weight)

    score = weight * confidence * criticality * recency * repetition
    parts = [f"{severity} severity"]
    if confidence < 0.85:
        parts.append(f"{confidence:.0%} confidence")
    if criticality > DEFAULT_CRITICALITY:
        parts.append(f"{zone_id} is a critical area (x{criticality:g})")
    if count > 1:
        parts.append(f"{count} occurrences")
    if recency < 0.6:
        parts.append(f"ageing (x{recency:.2f})")
    return Scored(
        score=round(score, 3),
        reason=", ".join(parts),
        factors={
            "severity": weight,
            "confidence": round(confidence, 3),
            "zone_criticality": criticality,
            "recency": round(recency, 3),
            "repetition": round(repetition, 3),
        },
    )


def _repetition_factor(count: int, weight: float) -> float:
    """How much repetition raises a score, capped so it cannot promote across a severity class.

    Logarithmic damping alone was not enough. Measured on the shipped weights: fifty medium-severity
    speeding events scored 19.7 and a fresh high-severity intrusion scored 15.1 — a chattering sensor
    outranking a real intrusion, which is precisely the failure that teaches operators to stop reading an
    inbox.

    Fifty repeats of a medium is a pattern worth noticing. It is not more urgent than something genuinely
    more serious, and a human triaging by hand would never make that swap. So repetition can lift an alert
    **toward** the next severity class and never past it: severity remains the primary sort, and repetition
    ranks within it.
    """
    if count <= 1:
        return 1.0
    raw = 1.0 + math.log1p(count - 1)
    ceiling = _next_severity_weight(weight) / weight
    return min(raw, ceiling)


def _next_severity_weight(weight: float) -> float:
    """The weight of the next severity class up, or a little headroom above the top one."""
    ordered = sorted(SEVERITY_WEIGHT.values())
    for value in ordered:
        if value > weight:
            return value
    # Already critical: allow a modest lift, because a critical repeating fifty times should still sort
    # above a critical that happened once.
    return weight * 1.5


def group_key(event: Event) -> str:
    """The dedup key: what counts as "the same alert happening again".

    Type plus location plus entity, in that order of coarseness. Getting this wrong fails in both
    directions and both are bad: too coarse and two genuinely different fires fold into one alert an
    operator resolves once; too fine and a chattering detector produces a hundred rows.

    Location before entity deliberately. A fire is about a *place*, and folding on the entity would give one
    alert per truck near the same fire.
    """
    location = event.zone_id or "site"
    entity = event.entities[0] if event.entities else ""
    # Entity is included only for event types that are genuinely about one thing. A fire in a zone is one
    # alert however many entities are nearby.
    per_entity = str(event.type) in (
        "speeding",
        "dwell_exceeded",
        "person_fell",
        "abandoned_package",
        "unauthorized_entry",
    )
    return f"{event.type}:{location}" + (f":{entity}" if per_entity and entity else "")


def title_for(event: Event) -> str:
    """A one-line title an operator can scan.

    Prefers the event's own explanation summary, which was written by the component that knew what happened,
    over anything reconstructed here. Falling back to the type and place is better than a generic
    "Alert" — an inbox of identical titles cannot be scanned at all.
    """
    summary = (event.explanation.summary or "").strip()
    if summary:
        return summary[:160]
    where = f" in {event.zone_id}" if event.zone_id else ""
    return f"{str(event.type).replace('_', ' ').title()}{where}"


def should_escalate(
    *,
    severity: Any,
    state: AlertState,
    ts: datetime,
    ack_ts: datetime | None,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Whether an unacknowledged alert has waited too long.

    Escalation is about the *response*, not the event: an unacknowledged critical is a process failure, and
    treating it as a more severe event would let a rising score substitute for somebody actually looking at
    it. So this is a separate decision with its own timer, and it fires once.
    """
    if state not in (AlertState.OPEN,):
        return False, None
    if ack_ts is not None:
        return False, None
    limit = ESCALATE_AFTER_S.get(str(severity))
    if limit is None:
        return False, None
    waited = ((now or utc_now()) - ts).total_seconds()
    if waited < limit:
        return False, None
    return True, (
        f"unacknowledged for {waited / 60:.0f} min ({severity!s} alerts escalate after "
        f"{limit / 60:.0f} min)"
    )


def within_dedup_window(last_ts: datetime, *, now: datetime | None = None) -> bool:
    return ((now or utc_now()) - last_ts) <= timedelta(seconds=DEDUP_WINDOW_S)


__all__ = [
    "DEDUP_WINDOW_S",
    "ESCALATE_AFTER_S",
    "RECENCY_HALF_LIFE_S",
    "SEVERITY_WEIGHT",
    "ZONE_CRITICALITY",
    "Scored",
    "group_key",
    "recency_factor",
    "score_alert",
    "severity_weight",
    "should_escalate",
    "title_for",
    "within_dedup_window",
    "zone_criticality",
]
