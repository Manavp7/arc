"""The rule engine: match, window and absence evaluation with cooldown (PRD M9).

Deliberately a plain synchronous class with no bus, no database and no clock of its own. Everything is
driven by the facts and timestamps handed to it, which makes the whole engine testable without
infrastructure — and an engine that is awkward to test is one whose rules nobody dares change.
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sio_core import get_logger

from .facts import Fact
from .rules import Rule, RuleSet

log = get_logger("sio.events.engine")


@dataclass
class Match:
    """A rule that fired, with everything needed to build an explainable event."""

    rule: Rule
    fact: Fact
    reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    subject: str = "*"
    aggregate_value: float | None = None
    contributing: list[Fact] = field(default_factory=list)

    @property
    def entity_ids(self) -> list[str]:
        ids = [self.fact.entity_id] if self.fact.entity_id else []
        for other in self.contributing:
            if other.entity_id and other.entity_id not in ids:
                ids.append(other.entity_id)
        return ids


class RuleEngine:
    """Evaluates a rule set against a stream of facts."""

    def __init__(self, ruleset: RuleSet, *, window_capacity: int = 2000) -> None:
        self.ruleset = ruleset
        self.window_capacity = window_capacity
        self._windows: dict[tuple[str, str], deque[Fact]] = defaultdict(
            lambda: deque(maxlen=window_capacity)
        )
        self._last_fired: dict[tuple[str, str], datetime] = {}
        self._last_seen: dict[tuple[str, str], datetime] = {}
        self._history_count: dict[tuple[str, str], int] = defaultdict(int)
        self._absence_fired: dict[tuple[str, str], datetime] = {}
        self.stats: dict[str, int] = defaultdict(int)

    def replace_rules(self, ruleset: RuleSet) -> None:
        """Swap the rule set without losing window state.

        Keeping the windows matters: a hot reload that cleared them would make every window rule blind
        for its whole window length, so editing an unrelated rule would silently suppress crowd
        detection for a minute.
        """
        self.ruleset = ruleset

    # ------------------------------------------------------------------ ingestion
    def evaluate(self, fact: Fact) -> list[Match]:
        """Evaluate one fact against every rule, returning whatever fired."""
        self.stats["facts"] += 1
        matches: list[Match] = []

        for rule in self.ruleset.enabled():
            if rule.kinds and fact.kind not in rule.kinds:
                continue
            if rule.shape == "match":
                match = self._evaluate_match(rule, fact)
                if match is not None:
                    matches.append(match)
            elif rule.shape == "window":
                match = self._evaluate_window(rule, fact)
                if match is not None:
                    matches.append(match)
            elif rule.shape == "absence":
                # Absence rules do not fire on arrival; they record that the subject is alive.
                self._note_alive(rule, fact)

        return matches

    def _conditions_hold(self, rule: Rule, fact: Fact) -> tuple[bool, list[str]]:
        reasons = []
        for condition in rule.when:
            actual = fact.get(condition.field_path)
            if not condition.evaluate(actual):
                return False, []
            reasons.append(condition.describe(actual))
        return True, reasons

    def _evaluate_match(self, rule: Rule, fact: Fact) -> Match | None:
        holds, reasons = self._conditions_hold(rule, fact)
        if not holds:
            return None
        subject = fact.keyed_by(rule.cooldown_key)
        if self._suppressed(rule, subject, fact.ts):
            return None
        self._record_fired(rule, subject, fact.ts)
        return Match(
            rule=rule,
            fact=fact,
            reasons=reasons,
            evidence=[fact.evidence_ref] if fact.evidence_ref else [],
            subject=subject,
        )

    def _evaluate_window(self, rule: Rule, fact: Fact) -> Match | None:
        """Aggregate recent facts for this subject, then test the aggregate.

        The window admits only facts that already satisfy ``when``, so ``when`` filters *membership*
        and the aggregate tests *the group*. That split is what lets "five or more people in a zone
        within a minute" be one rule instead of a special case.
        """
        assert rule.window is not None
        holds, reasons = self._conditions_hold(rule, fact)
        if not holds:
            return None

        subject = fact.keyed_by(rule.window.group_by)
        window = self._windows[(rule.id, subject)]
        window.append(fact)

        cutoff = fact.ts - timedelta(seconds=rule.window.seconds)
        while window and window[0].ts < cutoff:
            window.popleft()

        if len(window) < rule.window.min_samples:
            # Not enough evidence yet. Testing an aggregate over two samples and calling it a
            # ten-second window is how a rule fires on the first outlier it sees.
            self.stats["below_min_samples"] += 1
            return None

        value = self._aggregate(rule, list(window))
        if value is None or not rule.window_condition().evaluate(value):
            return None

        cooldown_subject = fact.keyed_by(rule.cooldown_key) if rule.cooldown_key else subject
        if self._suppressed(rule, cooldown_subject, fact.ts):
            return None
        self._record_fired(rule, cooldown_subject, fact.ts)

        aggregate_label = (
            f"{rule.window.aggregate}({rule.window.of})"
            if rule.window.of
            else rule.window.aggregate
        )
        reasons.append(
            f"{aggregate_label} over {rule.window.seconds:.0f}s for {subject or 'all'} was "
            f"{value:g} (needs {rule.window.op} {rule.window.value!r})"
        )
        return Match(
            rule=rule,
            fact=fact,
            reasons=reasons,
            evidence=[f.evidence_ref for f in window if f.evidence_ref][-8:],
            subject=subject,
            aggregate_value=value,
            contributing=list(window),
        )

    def _aggregate(self, rule: Rule, facts: list[Fact]) -> float | None:
        assert rule.window is not None
        spec = rule.window
        if spec.aggregate == "count":
            return float(len(facts))
        values: list[float] = []
        distinct: set[Any] = set()
        for fact in facts:
            raw = fact.get(spec.of or "")
            if raw is None:
                continue
            if spec.aggregate == "count_distinct":
                distinct.add(raw)
                continue
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                continue
        if spec.aggregate == "count_distinct":
            return float(len(distinct))
        if not values:
            return None
        return {
            "sum": sum,
            "max": max,
            "min": min,
            "mean": statistics.fmean,
            # The median is here because it is the right statistic for "is this vehicle actually
            # speeding": robust to a single wild fix by construction, rather than by hoping the window
            # is long enough to dilute it.
            "median": statistics.median,
        }[spec.aggregate](values)

    # -------------------------------------------------------------------- absence
    def _note_alive(self, rule: Rule, fact: Fact) -> None:
        subject = fact.keyed_by(rule.absence.group_by)  # type: ignore[union-attr]
        holds, _ = self._conditions_hold(rule, fact)
        if not holds:
            return
        key = (rule.id, subject)
        self._last_seen[key] = max(self._last_seen.get(key, fact.ts), fact.ts)
        self._history_count[key] += 1
        # A subject that reports again clears its fired flag, so a machine that stops, is fixed, and
        # stops again produces two events rather than one.
        self._absence_fired.pop(key, None)

    def check_absences(self, now: datetime) -> list[Match]:
        """Fire rules for subjects that have gone quiet.

        Called on a timer, because this is the one rule shape that cannot be driven by arriving
        messages: the whole point is that nothing arrived. A rule engine that only reacts to what it
        receives can never notice a machine that stopped, or a camera that went dark.
        """
        matches: list[Match] = []
        for rule in self.ruleset.by_shape("absence"):
            spec = rule.absence
            assert spec is not None
            for (rule_id, subject), last_seen in list(self._last_seen.items()):
                if rule_id != rule.id:
                    continue
                key = (rule_id, subject)
                if self._history_count[key] < spec.requires_history:
                    # Never established a baseline. Firing here would declare every machine stopped the
                    # moment the engine starts, which is how an absence rule gets muted permanently.
                    continue
                silence = (now - last_seen).total_seconds()
                if silence < spec.after_seconds:
                    continue
                if key in self._absence_fired:
                    continue
                self._absence_fired[key] = now
                self.stats["absences"] += 1
                from .facts import synthetic_fact

                fact = synthetic_fact(
                    "absence",
                    {
                        "subject": subject,
                        "source_id": subject,
                        "silence_s": round(silence, 1),
                        "last_seen": last_seen.isoformat(),
                        "reports_before_silence": self._history_count[key],
                    },
                    ts=now,
                    source_id=subject,
                )
                matches.append(
                    Match(
                        rule=rule,
                        fact=fact,
                        reasons=[
                            f"{subject} last reported {silence:.0f}s ago "
                            f"(threshold {spec.after_seconds:.0f}s)",
                            f"it had reported {self._history_count[key]} times before going quiet, "
                            "so this is a subject that was working",
                        ],
                        subject=subject,
                    )
                )
        return matches

    # ------------------------------------------------------------------ cooldown
    def _suppressed(self, rule: Rule, subject: str, ts: datetime) -> bool:
        if rule.cooldown_seconds <= 0:
            return False
        last = self._last_fired.get((rule.id, subject))
        if last is None:
            return False
        if (ts - last).total_seconds() >= rule.cooldown_seconds:
            return False
        self.stats["suppressed"] += 1
        return True

    def _record_fired(self, rule: Rule, subject: str, ts: datetime) -> None:
        self._last_fired[(rule.id, subject)] = ts
        self.stats["fired"] += 1
        self.stats[f"fired:{rule.id}"] += 1

    # -------------------------------------------------------------------- upkeep
    def prune(self, now: datetime, *, max_age_s: float = 3600.0) -> int:
        """Drop window and cooldown state nothing will need again.

        Without this the engine is a slow memory leak keyed by entity id, and entity ids are minted for
        every new object that appears on site.
        """
        dropped = 0
        cutoff = now - timedelta(seconds=max_age_s)
        for key, window in list(self._windows.items()):
            while window and window[0].ts < cutoff:
                window.popleft()
            if not window:
                del self._windows[key]
                dropped += 1
        for key, ts in list(self._last_fired.items()):
            if ts < cutoff:
                del self._last_fired[key]
                dropped += 1
        return dropped

    def describe(self) -> dict[str, Any]:
        return {
            "rules": len(self.ruleset.rules),
            "enabled": len(self.ruleset.enabled()),
            "by_shape": {
                shape: len(self.ruleset.by_shape(shape)) for shape in ("match", "window", "absence")
            },
            "errors": list(self.ruleset.errors),
            "windows": len(self._windows),
            "window_facts": sum(len(window) for window in self._windows.values()),
            "tracked_subjects": len(self._last_seen),
            "stats": dict(self.stats),
        }
