"""The rule language: declarative YAML over normalised facts (PRD M9, M22).

The acceptance criterion is that **adding a rule requires no core change**. That rules out a design
where each rule is a Python class, however tidy, because then adding one means editing, testing and
deploying the service. So a rule is a YAML document, and this module is the interpreter.

Pure data cannot express everything, and pretending otherwise produces a language that grows until it
is a bad programming language. The line drawn here:

* **YAML declares the rule** — what to match, over what window, how severe, how often it may fire.
* **A small set of primitives evaluates it** — comparison operators and four aggregates.

Adding a *rule* is a YAML file. Adding a genuinely new *kind of condition* is a new primitive, which is
a code change — and that is the honest boundary, because a new condition kind is new semantics, not
new configuration.

Three rule shapes, because the required rule list needs all three:

1. **match** — evaluate each fact as it arrives (`fire_detected`, `unauthorized_entry`).
2. **window** — aggregate recent facts per subject (`crowd_gathering`, `speeding`, `suspicious_meeting`).
3. **absence** — fire when an expected fact does *not* arrive (`machine_stopped`, `power_failure`).

Absence is the one people forget, and it is where the interesting failures live: a machine that stops
reporting is indistinguishable from a machine that is fine but whose network died, and a rule engine
that can only react to messages it receives can never notice either.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sio_core import get_logger

log = get_logger("sio.events.rules")

Operator = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "matches", "exists", "missing"
]
Aggregate = Literal["count", "count_distinct", "sum", "max", "min", "mean"]


class Condition(BaseModel):
    """One clause: a field, an operator, a value."""

    model_config = ConfigDict(extra="forbid")

    field_path: str = Field(alias="field")
    op: Operator = "eq"
    value: Any = None

    @field_validator("op", mode="before")
    @classmethod
    def _normalise(cls, value: Any) -> Any:
        # Accept the symbols people reach for first. A rule author writing ">=" and getting a
        # validation error learns nothing useful.
        symbols = {
            "==": "eq",
            "!=": "ne",
            ">": "gt",
            ">=": "gte",
            "<": "lt",
            "<=": "lte",
            "=~": "matches",
        }
        return symbols.get(str(value), value)

    def evaluate(self, actual: Any) -> bool:
        """Compare an actual value against this clause.

        Every comparison is defensive: a rule must never be able to crash the engine with a type
        mismatch, because the whole point of hot-reloadable rules is that a typo in a YAML file is a
        rule that does not match, not an outage.
        """
        expected = self.value
        try:
            if self.op == "exists":
                return actual is not None
            if self.op == "missing":
                return actual is None
            if actual is None:
                return False
            if self.op == "eq":
                return _coerce_equal(actual, expected)
            if self.op == "ne":
                return not _coerce_equal(actual, expected)
            if self.op in ("gt", "gte", "lt", "lte"):
                left, right = float(actual), float(expected)
                return {
                    "gt": left > right,
                    "gte": left >= right,
                    "lt": left < right,
                    "lte": left <= right,
                }[self.op]
            if self.op == "in":
                return any(_coerce_equal(actual, item) for item in _as_list(expected))
            if self.op == "not_in":
                return not any(_coerce_equal(actual, item) for item in _as_list(expected))
            if self.op == "contains":
                if isinstance(actual, (list, tuple, set)):
                    return any(_coerce_equal(item, expected) for item in actual)
                return str(expected) in str(actual)
            if self.op == "matches":
                return re.search(str(expected), str(actual)) is not None
        except (TypeError, ValueError):
            return False
        return False

    def describe(self, actual: Any) -> str:
        """Human-readable clause with the value that actually occurred.

        An explanation reading "speed_kmh gte 20" is barely better than nothing; one reading
        "speed_kmh was 31.4 (needs >= 20)" tells an operator whether to believe it.
        """
        symbol = {
            "eq": "==",
            "ne": "!=",
            "gt": ">",
            "gte": ">=",
            "lt": "<",
            "lte": "<=",
            "in": "in",
            "not_in": "not in",
            "contains": "contains",
            "matches": "matches",
            "exists": "is present",
            "missing": "is absent",
        }[self.op]
        if self.op in ("exists", "missing"):
            return f"{self.field_path} {symbol}"
        return f"{self.field_path} was {actual!r} (needs {symbol} {self.value!r})"


class WindowSpec(BaseModel):
    """An aggregate over recent facts, grouped by subject."""

    model_config = ConfigDict(extra="forbid")

    aggregate: Aggregate = "count"
    of: str | None = Field(default=None, description="Field to aggregate; unused by count")
    seconds: float = Field(default=60.0, gt=0)
    group_by: tuple[str, ...] = ("zone_id",)
    """What counts as one subject. Crowd gathering groups by zone; speeding groups by entity."""
    op: Operator = "gte"
    value: Any = 1

    @field_validator("group_by", mode="before")
    @classmethod
    def _tupleise(cls, value: Any) -> Any:
        if isinstance(value, str):
            return (value,)
        return tuple(value) if value else ()

    @model_validator(mode="after")
    def _needs_field_for_value_aggregates(self) -> WindowSpec:
        if self.aggregate != "count" and not self.of:
            raise ValueError(f"aggregate {self.aggregate!r} needs an 'of' field")
        return self


class AbsenceSpec(BaseModel):
    """Fire when a subject that *was* reporting stops.

    ``after_seconds`` is the silence that counts as a fault. ``requires_history`` stops the rule firing
    for a subject that has never reported at all — otherwise every machine on the site is "stopped"
    the moment the engine starts, which is the classic way an absence rule gets switched off and never
    switched back on.
    """

    model_config = ConfigDict(extra="forbid")

    after_seconds: float = Field(default=300.0, gt=0)
    group_by: tuple[str, ...] = ("source_id",)
    requires_history: int = Field(default=2, ge=1)

    @field_validator("group_by", mode="before")
    @classmethod
    def _tupleise(cls, value: Any) -> Any:
        return (value,) if isinstance(value, str) else tuple(value)


class Rule(BaseModel):
    """A rule as authored in YAML."""

    model_config = ConfigDict(extra="forbid")

    id: str
    emits: str = Field(description="EventType the rule asserts")
    severity: str = "info"
    enabled: bool = True
    description: str = ""
    kinds: tuple[str, ...] = ()
    """Fact kinds this rule looks at. Empty means all — allowed, but narrowing is cheaper."""
    when: list[Condition] = Field(default_factory=list)
    """All must hold. An empty list matches everything of the right kind, which is occasionally what
    an absence rule wants."""
    window: WindowSpec | None = None
    absence: AbsenceSpec | None = None
    cooldown_seconds: float = Field(default=60.0, ge=0)
    """Minimum gap between firings for the same subject.

    Not optional in practice. A condition that is true of a parked truck is true of every message about
    it, and without a cooldown one stationary vehicle in a restricted zone produces an event per second
    until someone deletes the rule."""
    cooldown_key: tuple[str, ...] = ("entity_id", "zone_id")
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    attributes: dict[str, Any] = Field(default_factory=dict)
    explanation: str = ""
    """A sentence for the operator. Supports ``{field}`` interpolation from the matched fact."""

    @field_validator("kinds", "cooldown_key", mode="before")
    @classmethod
    def _tupleise(cls, value: Any) -> Any:
        if value is None:
            return ()
        return (value,) if isinstance(value, str) else tuple(value)

    @model_validator(mode="after")
    def _one_shape_only(self) -> Rule:
        if self.window and self.absence:
            raise ValueError(
                f"rule {self.id!r} declares both window and absence; a rule has one shape"
            )
        return self

    @property
    def shape(self) -> str:
        if self.absence:
            return "absence"
        return "window" if self.window else "match"

    def window_condition(self) -> Condition:
        """The window's threshold expressed as an ordinary condition.

        Reusing `Condition` rather than writing a second comparison path means the operators behave
        identically whether they appear in ``when`` or in ``window`` — including the symbol aliases and
        the type coercion. Two implementations of ``gte`` would eventually differ, and the difference
        would be invisible in the YAML.
        """
        assert self.window is not None
        return Condition.model_validate(
            {"field": f"{self.window.aggregate}", "op": self.window.op, "value": self.window.value}
        )


@dataclass
class RuleSet:
    """Rules loaded from disk, with the errors that stopped some of them loading."""

    rules: list[Rule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    loaded_from: list[str] = field(default_factory=list)
    fingerprint: str = ""

    def enabled(self) -> list[Rule]:
        return [rule for rule in self.rules if rule.enabled]

    def by_shape(self, shape: str) -> list[Rule]:
        return [rule for rule in self.enabled() if rule.shape == shape]

    def get(self, rule_id: str) -> Rule | None:
        return next((rule for rule in self.rules if rule.id == rule_id), None)


def load_rules(directory: Path) -> RuleSet:
    """Load every rule in a directory.

    **One bad rule must not take down the others.** A YAML typo is a routine event, and a loader that
    raises on the first problem means a single malformed file silently disables the fire rule. Errors
    are collected, reported in ``/health``, and the rest of the rules load.
    """
    ruleset = RuleSet()
    if not directory.exists():
        ruleset.errors.append(f"rules directory not found: {directory}")
        return ruleset

    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.y*ml")):
        try:
            documents = list(yaml.safe_load_all(path.read_text()))
        except yaml.YAMLError as exc:
            ruleset.errors.append(f"{path.name}: invalid YAML: {exc}")
            continue
        for document in documents:
            if not document:
                continue
            entries = document.get("rules", [document]) if isinstance(document, dict) else document
            for entry in _as_list(entries):
                try:
                    rule = Rule.model_validate(entry)
                except Exception as exc:
                    ruleset.errors.append(f"{path.name}: {_short_error(exc)}")
                    continue
                if rule.id in seen:
                    # Duplicate ids are rejected rather than resolved by order, because "the last one
                    # wins" is a rule nobody can see when reading either file.
                    ruleset.errors.append(
                        f"{path.name}: duplicate rule id {rule.id!r} (already in {seen[rule.id]})"
                    )
                    continue
                seen[rule.id] = path.name
                ruleset.rules.append(rule)
        ruleset.loaded_from.append(path.name)

    ruleset.fingerprint = fingerprint_of(directory)
    return ruleset


def fingerprint_of(directory: Path) -> str:
    """A cheap signature of the rules on disk, for hot reload.

    Modification time and size rather than a content hash: reloading is idempotent and cheap, so a
    false positive costs nothing, while hashing every file on a timer costs something on every tick.
    """
    if not directory.exists():
        return ""
    parts = [
        f"{path.name}:{path.stat().st_mtime_ns}:{path.stat().st_size}"
        for path in sorted(directory.glob("*.y*ml"))
    ]
    return "|".join(parts)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _coerce_equal(actual: Any, expected: Any) -> bool:
    """Equality that survives YAML's typing.

    ``severity: high`` is a string; ``value: 20`` is an int; a field holding "20" and a rule holding 20
    mean the same thing to the person who wrote them, and a rule engine that disagrees on that will be
    described as broken — correctly.
    """
    if actual == expected:
        return True
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(actual) == bool(expected)
    try:
        return float(actual) == float(expected)
    except (TypeError, ValueError):
        return str(actual).lower() == str(expected).lower()


def _short_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return text[:200]


def describe_rules(rules: Iterable[Rule]) -> list[dict[str, Any]]:
    return [
        {
            "id": rule.id,
            "emits": rule.emits,
            "severity": rule.severity,
            "shape": rule.shape,
            "enabled": rule.enabled,
            "kinds": list(rule.kinds),
            "conditions": len(rule.when),
            "cooldown_s": rule.cooldown_seconds,
            "description": rule.description,
        }
        for rule in rules
    ]
