"""Workflows defined as JSON, not Python (PRD M22, Phase 6).

The plan calls this a "no-code builder", and the honest framing is narrower: it lets somebody compose the
*existing* activities into a new response without a deploy. It does not let them invent an activity — dispatching
a drone is code, and pretending otherwise would be the kind of no-code promise that ends with a YAML file
containing a Python expression.

**Validation is the entire product.** Parsing JSON into steps is twenty lines; anybody can do that. The value is
refusing a workflow that would fail at 3am during a fire, and saying exactly why. A builder that accepts a
workflow referencing an activity that does not exist has not saved anybody a deploy — it has moved the failure
from a code review to an incident.

So `validate()` returns *problems*, plural, each naming what is wrong and what the valid options are. Not the
first error: somebody fixing a workflow wants the list, and a validator that reveals one problem per attempt
turns a five-minute task into twenty.

The spec::

    {
      "name": "SpillResponse",
      "trigger": {"event_types": ["spill_detected"], "min_severity": "high", "cooldown_s": 600},
      "conditions": [{"field": "payload.zone_id", "op": "in", "value": ["fuel_store", "dock_1"]}],
      "steps": [
        {"id": "notify", "activity": "notify_security", "arguments": {"channel": "ops"}},
        {"id": "contain", "activity": "close_gate", "after": ["notify"], "compensate": "open_gate"}
      ]
    }

`after` is what makes it a DAG rather than a list. The engine runs steps in order and compensates in reverse, so
the DAG is **topologically sorted into that order** — see `_topological_order`, which also rejects cycles. A
graph that cannot be linearised cannot be compensated in reverse, and a partial rollback is worse than none
because nobody can tell what state the site is in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .activities import ACTIVITIES
from .playbooks import Playbook, StepSpec

SEVERITIES = ("info", "low", "medium", "high", "critical")

#: Fields that legitimately sit at the top level of a fact.
#:
#: Needed because the "did you mean payload.x?" hint below is otherwise a false positive on exactly the fields the
#: editor offers. The first version flagged every dotless field, so `zone_id` — which the vocabulary endpoint
#: advertises — came back as a problem. A validator that complains about its own suggestions is one people learn
#: to ignore, which costs far more than the hint is worth.
TOP_LEVEL_FIELDS = ("type", "severity", "zone_id", "entity_id", "rule_id", "event_id")

#: Conditions are a fixed vocabulary of comparisons, deliberately.
#:
#: The tempting alternative is an expression string, and the moment one exists somebody puts
#: `payload.count > 3 and zone in zones` in it and now the platform has an interpreter, a sandbox problem and an
#: injection surface — for a feature whose entire point was to avoid writing code.
OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "exists")

#: An upper bound on steps.
#:
#: Not because 24 is special, but because a no-code editor with no limit eventually receives a generated file
#: with four thousand steps, and the failure then looks like the workflow engine being broken.
MAX_STEPS = 24


@dataclass
class Problem:
    """One thing wrong with a workflow, and what to do about it."""

    where: str
    """Where it is, in a form a UI can highlight — `steps[2].activity`."""
    message: str
    fix: str | None = None

    def describe(self) -> dict[str, Any]:
        return {"where": self.where, "message": self.message, "fix": self.fix}


@dataclass
class Condition:
    """A guard on the triggering event.

    Separate from the trigger because "runs on fire_detected" and "only in the fuel store" are different
    thoughts, and a UI that conflates them produces a trigger list nobody can read.
    """

    field_path: str
    op: str
    value: Any = None

    def evaluate(self, fact: dict[str, Any]) -> bool:
        actual = _dig(fact, self.field_path)
        if self.op == "exists":
            return actual is not None
        if actual is None:
            # A missing field fails every comparison except `exists`. The alternative — treating absence as a
            # zero or an empty string — makes `payload.count lt 5` true for an event that has no count, which is
            # how a workflow fires on something it was never meant to see.
            return False
        try:
            match self.op:
                case "eq":
                    return bool(actual == self.value)
                case "ne":
                    return bool(actual != self.value)
                case "gt":
                    return float(actual) > float(self.value)
                case "gte":
                    return float(actual) >= float(self.value)
                case "lt":
                    return float(actual) < float(self.value)
                case "lte":
                    return float(actual) <= float(self.value)
                case "in":
                    return actual in (self.value or [])
                case "not_in":
                    return actual not in (self.value or [])
                case "contains":
                    return str(self.value) in str(actual)
        except (TypeError, ValueError):
            # A comparison between incompatible types is a false, not a crash. The workflow author made a
            # mistake, and the correct response is "this did not match" plus a log line — not tearing down the
            # consumer for every other workflow.
            return False
        return False

    def describe(self) -> dict[str, Any]:
        return {"field": self.field_path, "op": self.op, "value": self.value}


@dataclass
class WorkflowSpec:
    """A workflow as authored: trigger, conditions, steps."""

    name: str
    description: str = ""
    event_types: tuple[str, ...] = ()
    min_severity: str = "high"
    cooldown_s: float = 300.0
    key_by: tuple[str, ...] = ("zone_id",)
    conditions: tuple[Condition, ...] = ()
    steps: tuple[dict[str, Any], ...] = ()
    enabled: bool = True
    source: str = "no-code"

    def matches(self, fact: dict[str, Any]) -> bool:
        """Whether this workflow's conditions pass for a fact.

        The trigger (event type, severity) is checked by the engine, as it is for code playbooks. This is only
        the extra guard, so a no-code workflow cannot be triggered by something a code playbook could not.
        """
        return all(condition.evaluate(fact) for condition in self.conditions)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "trigger": {
                "event_types": list(self.event_types),
                "min_severity": self.min_severity,
                "cooldown_s": self.cooldown_s,
                "key_by": list(self.key_by),
            },
            "conditions": [condition.describe() for condition in self.conditions],
            "steps": list(self.steps),
            "enabled": self.enabled,
            "source": self.source,
        }


def parse(document: dict[str, Any]) -> WorkflowSpec:
    """Read a spec, tolerantly. Validation is a separate step and reports everything at once.

    Tolerant on purpose: a parser that raises on the first missing key can only ever report one problem, and the
    author would then fix it and discover the next. `validate` exists to produce the whole list.
    """
    trigger = document.get("trigger") or {}
    conditions = tuple(
        Condition(
            field_path=str(item.get("field", "")),
            op=str(item.get("op", "eq")),
            value=item.get("value"),
        )
        for item in (document.get("conditions") or [])
        if isinstance(item, dict)
    )
    return WorkflowSpec(
        name=str(document.get("name", "")),
        description=str(document.get("description", "")),
        event_types=tuple(str(item) for item in (trigger.get("event_types") or [])),
        min_severity=str(trigger.get("min_severity", "high")),
        cooldown_s=float(trigger.get("cooldown_s", 300.0) or 0.0),
        key_by=tuple(str(item) for item in (trigger.get("key_by") or ("zone_id",))),
        conditions=conditions,
        steps=tuple(item for item in (document.get("steps") or []) if isinstance(item, dict)),
        enabled=bool(document.get("enabled", True)),
        source=str(document.get("source", "no-code")),
    )


def validate(spec: WorkflowSpec) -> list[Problem]:
    """Every problem with a workflow, each naming its fix.

    Everything, not the first: somebody fixing a workflow wants the list, and a validator revealing one problem
    per attempt turns a five-minute task into twenty.
    """
    problems: list[Problem] = []

    if not spec.name:
        problems.append(
            Problem("name", "a workflow needs a name", "give it one, e.g. 'SpillResponse'")
        )
    elif not spec.name.replace("_", "").replace("-", "").isalnum():
        problems.append(
            Problem(
                "name",
                f"the name {spec.name!r} has characters other than letters, digits, - and _",
                "names appear in run records and metric labels, where punctuation causes trouble later",
            )
        )

    if not spec.event_types:
        problems.append(
            Problem(
                "trigger.event_types",
                "nothing triggers this workflow, so it will never run",
                "add at least one event type, e.g. 'fire_detected'",
            )
        )

    if spec.min_severity not in SEVERITIES:
        problems.append(
            Problem(
                "trigger.min_severity",
                f"{spec.min_severity!r} is not a severity",
                f"one of: {', '.join(SEVERITIES)}",
            )
        )

    if spec.cooldown_s < 0:
        problems.append(
            Problem("trigger.cooldown_s", "a cooldown cannot be negative", "use 0 for none")
        )
    elif spec.cooldown_s == 0 and spec.event_types:
        # A warning that has to be a problem, because the consequence is severe and invisible in testing.
        problems.append(
            Problem(
                "trigger.cooldown_s",
                "with no cooldown, a detector that fires on every frame will start a run per frame",
                "a fire produces fire_detected continuously while it burns; 300s is a sane floor",
            )
        )

    # --- conditions ---
    for index, condition in enumerate(spec.conditions):
        if not condition.field_path:
            problems.append(
                Problem(f"conditions[{index}].field", "no field to test", "e.g. 'payload.zone_id'")
            )
        if condition.op not in OPERATORS:
            problems.append(
                Problem(
                    f"conditions[{index}].op",
                    f"{condition.op!r} is not a comparison this engine knows",
                    f"one of: {', '.join(OPERATORS)}",
                )
            )
        if condition.op in ("in", "not_in") and not isinstance(condition.value, list):
            problems.append(
                Problem(
                    f"conditions[{index}].value",
                    f"{condition.op!r} needs a list to test against",
                    "e.g. ['fuel_store', 'dock_1']",
                )
            )
        if (
            condition.field_path
            and "." not in condition.field_path
            and condition.field_path not in TOP_LEVEL_FIELDS
        ):
            # The single most common authoring mistake, learned the hard way in the plugin rule: the fields
            # people mean are nested under `payload`, and a bare name silently matches nothing. Scoped to
            # UNKNOWN dotless names, because `zone_id` and `severity` really are top-level and flagging them
            # would make the hint noise.
            problems.append(
                Problem(
                    f"conditions[{index}].field",
                    f"{condition.field_path!r} is not a top-level field of an event",
                    f"sensor and detection values live under payload — did you mean "
                    f"'payload.{condition.field_path}'? Top-level fields are: "
                    f"{', '.join(TOP_LEVEL_FIELDS)}",
                )
            )

    # --- steps ---
    if not spec.steps:
        problems.append(
            Problem("steps", "a workflow with no steps does nothing", "add at least one")
        )
    if len(spec.steps) > MAX_STEPS:
        problems.append(
            Problem(
                "steps",
                f"{len(spec.steps)} steps is more than the {MAX_STEPS} this engine will run",
                "split it, or drive it from a code playbook",
            )
        )

    seen: set[str] = set()
    for index, step in enumerate(spec.steps):
        step_id = str(step.get("id", ""))
        where = f"steps[{index}]"
        if not step_id:
            problems.append(
                Problem(f"{where}.id", "a step needs an id", "used to reference it in `after`")
            )
        elif step_id in seen:
            problems.append(
                Problem(
                    f"{where}.id",
                    f"two steps share the id {step_id!r}",
                    "`after` could not tell them apart, and neither can a run record",
                )
            )
        seen.add(step_id)

        activity = str(step.get("activity", ""))
        if not activity:
            problems.append(
                Problem(f"{where}.activity", "a step needs an activity", f"one of: {_known()}")
            )
        elif activity not in ACTIVITIES:
            problems.append(
                Problem(
                    f"{where}.activity",
                    f"there is no activity called {activity!r}",
                    # Naming the options rather than saying "invalid" — the same lesson the copilot's tools
                    # taught: an error that lists what is valid is one nobody has to ask about.
                    f"available: {_known()}. Adding a new activity is a code change, deliberately",
                )
            )

        compensate = step.get("compensate")
        if compensate and str(compensate) not in ACTIVITIES:
            problems.append(
                Problem(
                    f"{where}.compensate",
                    f"there is no activity called {compensate!r} to undo this step",
                    f"available: {_known()}",
                )
            )
        if compensate and step.get("irreversible"):
            problems.append(
                Problem(
                    f"{where}",
                    "a step cannot be both compensable and irreversible",
                    "drop one; 'nothing to undo' and 'cannot be undone' are different facts",
                )
            )

    # --- the graph ---
    for index, step in enumerate(spec.steps):
        for dependency in step.get("after") or []:
            if str(dependency) not in seen:
                problems.append(
                    Problem(
                        f"steps[{index}].after",
                        f"depends on {dependency!r}, which is not a step in this workflow",
                        f"steps present: {sorted(identifier for identifier in seen if identifier)}",
                    )
                )

    if not any(problem.where.startswith("steps") for problem in problems):
        cycle = _find_cycle(spec.steps)
        if cycle:
            problems.append(
                Problem(
                    "steps.after",
                    f"these steps depend on each other in a loop: {' → '.join(cycle)}",
                    # Not a pedantic graph-theory objection: compensation runs in reverse order, and a graph with
                    # a cycle has no order to reverse.
                    "a loop has no order to run in, and compensation runs in reverse order",
                )
            )

    return problems


def to_playbook(spec: WorkflowSpec) -> Playbook:
    """Translate a validated spec into the same `Playbook` the code playbooks produce.

    One engine, not two. A separate execution path for no-code workflows would mean retries, compensation,
    cooldowns and run records all had a second implementation that drifts — and the no-code one would be the
    less-tested of the pair while running the same drones.
    """
    problems = validate(spec)
    if problems:
        raise ValueError(
            f"cannot translate an invalid workflow: {'; '.join(problem.message for problem in problems)}"
        )

    ordered = _topological_order(spec.steps)
    steps = tuple(
        StepSpec(
            step_id=str(step["id"]),
            name=str(step.get("name") or f"{step['activity']} ({step['id']})"),
            activity=str(step["activity"]),
            arguments=dict(step.get("arguments") or {}),
            timeout_s=float(step.get("timeout_s", 20.0)),
            max_attempts=int(step.get("max_attempts", 3)),
            backoff_s=float(step.get("backoff_s", 1.0)),
            compensate=str(step["compensate"]) if step.get("compensate") else None,
            irreversible=bool(step.get("irreversible", False)),
            optional=bool(step.get("optional", False)),
        )
        for step in ordered
    )
    return Playbook(
        name=spec.name,
        description=spec.description or f"Authored workflow: {spec.name}",
        steps=steps,
        trigger_event_types=spec.event_types,
        trigger_min_severity=spec.min_severity,
        cooldown_s=spec.cooldown_s,
        key_by=spec.key_by,
    )


# ------------------------------------------------------------------------- graph
def _topological_order(steps: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Linearise the DAG, preserving authoring order among independent steps.

    Kahn's algorithm with a stable tie-break, which matters for a UI: two steps with no dependency between them
    should run in the order they appear on screen. An unstable sort would make the same workflow run differently
    between saves, and the author would have no way to explain it.
    """
    by_id = {str(step.get("id")): step for step in steps}
    pending = {
        identifier: [
            str(dependency) for dependency in (step.get("after") or []) if str(dependency) in by_id
        ]
        for identifier, step in by_id.items()
    }
    order: list[dict[str, Any]] = []
    remaining = dict(pending)
    while remaining:
        ready = [
            identifier
            for identifier in by_id
            if identifier in remaining and not remaining[identifier]
        ]
        if not ready:
            break  # a cycle; `validate` reports it, and this must not spin
        for identifier in ready:
            order.append(by_id[identifier])
            remaining.pop(identifier)
        for dependencies in remaining.values():
            for identifier in ready:
                if identifier in dependencies:
                    dependencies.remove(identifier)
    return order


def _find_cycle(steps: tuple[dict[str, Any], ...]) -> list[str]:
    """A cycle, as a readable path, or empty.

    The path rather than a boolean, because "there is a cycle" leaves the author to find it in a graph they
    cannot see. `a → b → c → a` is actionable.
    """
    graph = {
        str(step.get("id")): [str(dependency) for dependency in (step.get("after") or [])]
        for step in steps
    }
    state: dict[str, int] = {}
    path: list[str] = []

    def walk(node: str) -> list[str]:
        if state.get(node) == 1:
            return [*path[path.index(node) :], node]
        if state.get(node) == 2 or node not in graph:
            return []
        state[node] = 1
        path.append(node)
        for neighbour in graph[node]:
            found = walk(neighbour)
            if found:
                return found
        path.pop()
        state[node] = 2
        return []

    for node in graph:
        found = walk(node)
        if found:
            return found
    return []


def _dig(fact: dict[str, Any], path: str) -> Any:
    current: Any = fact
    for part in path.split("."):
        current = current.get(part) if isinstance(current, dict) else getattr(current, part, None)
        if current is None:
            return None
    return current


def _known() -> str:
    return ", ".join(sorted(ACTIVITIES))


# ------------------------------------------------------------------------ storage
def load_workflows(directory: Path) -> tuple[list[WorkflowSpec], list[Problem]]:
    """Every valid workflow on disk, and every problem found.

    Both, because the alternative is a service that either refuses to start over one bad file or silently
    ignores it. Neither is acceptable: the first makes one typo an outage, the second makes a workflow that
    never runs indistinguishable from one that never fires.
    """
    specs: list[WorkflowSpec] = []
    problems: list[Problem] = []
    if not directory.exists():
        return specs, problems

    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text())
        except json.JSONDecodeError as error:
            problems.append(
                Problem(path.name, f"not valid JSON: {error}", "check for a trailing comma")
            )
            continue
        spec = parse(document)
        found = validate(spec)
        if found:
            problems.extend(
                Problem(f"{path.name}:{problem.where}", problem.message, problem.fix)
                for problem in found
            )
            continue
        specs.append(spec)
    return specs, problems


__all__ = [
    "MAX_STEPS",
    "OPERATORS",
    "SEVERITIES",
    "TOP_LEVEL_FIELDS",
    "Condition",
    "Problem",
    "WorkflowSpec",
    "load_workflows",
    "parse",
    "to_playbook",
    "validate",
]
