#!/usr/bin/env python
"""Score candidate Ollama models on THIS copilot's tool-calling task, and pin the winner.

**Model selection is a tool-calling decision, not a fluency decision.** A 1.5-3 B model that chats well
but cannot reliably pick among nine tools presents to a user as a broken product, not as a weak model —
the failure is fluent and therefore invisible. So the choice is made by measurement, on the fixture the
product actually needs, and the result is written to `docs/MODELS.md` so the decision can be re-checked
rather than believed.

What is scored, and why each part is separate:

* **selection** — did it choose the right tool? The headline number.
* **arguments** — were the required arguments present and correct? A right tool with a wrong zone answers
  a different question, fluently.
* **restraint** — did it decline to call a tool when none was needed? Measured separately because it is
  the axis small models fail worst, and an eager model degrades the whole product: if "hello" triggers a
  database query, nobody trusts the answer to a real question.
* **latency** — the p50 and p95, because the PRD's budget is under ten seconds end to end and a model
  that is right after thirty is not usable.

Published benchmarks informed the candidate list; they cannot make this decision, because a general
fixture measures a general task and this is not one.

Usage:
    uv run python scripts/eval_tool_calling.py                        # all installed candidates
    uv run python scripts/eval_tool_calling.py --models qwen3:1.7b    # just one
    uv run python scripts/eval_tool_calling.py --write-docs           # update docs/MODELS.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_schemas" / "src"))
sys.path.insert(0, str(REPO_ROOT / "services" / "copilot" / "src"))

from sio_copilot.evalset import EVAL_CASES  # noqa: E402
from sio_copilot.tools import ToolBelt  # noqa: E402

from sio_core.llm import OllamaLLM  # noqa: E402
from sio_core.llm.base import validate_arguments  # noqa: E402

# The candidates, drawn from the 1.5-3 B band the PRD calls for plus two deliberate controls: a 0.6 B to
# show where the floor is, and a 3 B from a different family to check the result is not Qwen-specific.
CANDIDATES = (
    "qwen3:1.7b",
    "qwen2.5:1.5b",
    "qwen2.5:3b",
    "llama3.2:3b",
    "granite4:3b",
    "qwen3:0.6b",
)

# THE PROMPT THE PRODUCT USES, imported rather than copied.
#
# The first version of this script had its own prompt, which happened to contain an explicit restraint
# instruction the agent's prompt lacked. Every model therefore scored 100 % on restraint here, while the
# shipped copilot called `list_entities` to answer "Hello." A score measured against a prompt nobody ships
# is folklore. Importing it means the harness cannot drift from the product, and a unit test asserts it.
from sio_copilot.agent import SYSTEM_PROMPT  # noqa: E402


@dataclass
class CaseOutcome:
    case_id: str
    expected: str | None
    chose: str | None
    selection_ok: bool
    arguments_ok: bool
    latency_ms: float
    detail: str = ""


@dataclass
class ModelScore:
    model: str
    outcomes: list[CaseOutcome] = field(default_factory=list)
    error: str | None = None

    @property
    def action_cases(self) -> list[CaseOutcome]:
        return [outcome for outcome in self.outcomes if outcome.expected is not None]

    @property
    def restraint_cases(self) -> list[CaseOutcome]:
        return [outcome for outcome in self.outcomes if outcome.expected is None]

    @property
    def selection(self) -> float:
        cases = self.action_cases
        return sum(outcome.selection_ok for outcome in cases) / len(cases) if cases else 0.0

    @property
    def arguments(self) -> float:
        """Argument accuracy over the cases whose tool was chosen correctly.

        Conditioned on selection deliberately: penalising arguments for a tool that was never going to be
        called would double-count one mistake and hide whether a model that picks well also fills in well.
        """
        cases = [outcome for outcome in self.action_cases if outcome.selection_ok]
        return sum(outcome.arguments_ok for outcome in cases) / len(cases) if cases else 0.0

    @property
    def restraint(self) -> float:
        cases = self.restraint_cases
        return sum(outcome.selection_ok for outcome in cases) / len(cases) if cases else 0.0

    @property
    def latencies(self) -> list[float]:
        return sorted(outcome.latency_ms for outcome in self.outcomes if outcome.latency_ms > 0)

    @property
    def p50_ms(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0

    @property
    def p95_ms(self) -> float:
        values = self.latencies
        if not values:
            return 0.0
        return values[min(len(values) - 1, int(len(values) * 0.95))]

    @property
    def overall(self) -> float:
        """One number for ranking, weighted by what the PRODUCT depends on.

        Selection dominates, because a wrong tool is a wrong answer and no amount of code around the model
        can recover from it. Arguments matter next: a right tool with a wrong zone answers a different
        question, fluently.

        **Restraint is weighted lightly, and that is a deliberate change.** It used to carry a fifth of the
        score, until measurement showed the best candidate still queries the database to answer "Hello" one
        time in three — and that the score moved with the prompt rather than being a stable property of the
        model. Restraint is now decided in code (`agent.conversational_reply`), because a greeting is
        trivially recognisable and there is no version of "hello" whose right answer involves the world
        model. It is still reported, because a model with poor restraint is a model to be careful with, but
        the product no longer bets on it.

        Latency is a gate rather than a term: past ten seconds a model is disqualified whatever its
        accuracy. That is a cliff, not a gradient.
        """
        if self.error:
            return 0.0
        score = 0.7 * self.selection + 0.25 * self.arguments + 0.05 * self.restraint
        if self.p95_ms > 10_000:
            score *= 0.5
        return round(score, 4)

    @property
    def usable(self) -> bool:
        """Meets the PRD's bar: >= 90 % selection and a p95 inside the latency budget.

        Restraint is not in the bar, because the product no longer depends on it — see `overall`.
        """
        return self.selection >= 0.9 and self.p95_ms <= 10_000 and not self.error

    def row(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "selection": round(self.selection, 3),
            "arguments": round(self.arguments, 3),
            "restraint": round(self.restraint, 3),
            "p50_ms": round(self.p50_ms),
            "p95_ms": round(self.p95_ms),
            "overall": self.overall,
            "usable": self.usable,
            "error": self.error,
        }


async def score_model(model: str, specs: list[Any], *, timeout_s: float = 90.0) -> ModelScore:
    llm = OllamaLLM(model=model, timeout_s=timeout_s, temperature=0.0)
    score = ModelScore(model=model)
    if not await llm.ping():
        score.error = "ollama unreachable"
        await llm.close()
        return score

    by_name = {spec.name: spec for spec in specs}
    for case in EVAL_CASES:
        started = time.perf_counter()
        reply = await llm.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": case.question},
            ],
            tools=specs,
        )
        elapsed = (time.perf_counter() - started) * 1000
        chose = reply.tool_calls[0].name if reply.tool_calls else None

        if case.expect_tool is None:
            selection_ok = chose is None
            arguments_ok = selection_ok
            detail = "" if selection_ok else f"called {chose} when no tool was needed"
        else:
            selection_ok = chose == case.expect_tool or chose in case.acceptable_tools
            arguments_ok = False
            detail = ""
            if selection_ok and reply.tool_calls:
                call = reply.tool_calls[0]
                spec = by_name.get(call.name)
                schema_ok, problem = (
                    validate_arguments(call, spec) if spec else (False, "unknown tool")
                )
                wanted = case.expect_arguments
                matched = all(
                    str(call.arguments.get(key, "")).lower() == str(value).lower()
                    or (isinstance(value, bool) and bool(call.arguments.get(key)) == value)
                    or (
                        isinstance(value, (int, float))
                        and _as_number(call.arguments.get(key)) == float(value)
                    )
                    for key, value in wanted.items()
                )
                arguments_ok = bool(schema_ok and matched)
                if not arguments_ok:
                    detail = f"args {call.arguments} vs wanted {wanted}" + (
                        f" ({problem})" if problem else ""
                    )
            elif not selection_ok:
                detail = f"chose {chose or 'nothing'}"

        score.outcomes.append(
            CaseOutcome(
                case_id=case.id,
                expected=case.expect_tool,
                chose=chose,
                selection_ok=selection_ok,
                arguments_ok=arguments_ok,
                latency_ms=elapsed,
                detail=detail,
            )
        )
    await llm.close()
    return score


def _as_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def render_docs(scores: list[ModelScore], *, fixture_size: int) -> str:
    ranked = sorted(scores, key=lambda score: -score.overall)
    winner = next((score for score in ranked if score.usable), ranked[0] if ranked else None)
    lines = [
        "# Model selection",
        "",
        "Generated by `scripts/eval_tool_calling.py`. Re-run it after changing the tool set — a tool",
        "added or reworded changes the task, and a score measured against the old task is folklore.",
        "",
        "## Why this is measured rather than chosen",
        "",
        "A 1.5-3 B model that chats well but cannot reliably pick among nine tools presents to a user as a",
        "broken product, not as a weak model: the failure is fluent, and therefore invisible. Published",
        "benchmarks informed the candidate list, but they measure a general task and this is not one.",
        "",
        "## What is scored",
        "",
        "| axis | meaning | why it is separate |",
        "|---|---|---|",
        "| **selection** | chose the right tool | a wrong tool is a wrong answer, delivered confidently |",
        "| **arguments** | required arguments present and correct | a right tool with a wrong zone answers a different question |",
        "| **restraint** | declined to call a tool when none was needed | reported, but no longer bet on: see below |",
        "| **latency** | p50 and p95 per question | the budget is under ten seconds end to end |",
        "",
        "Arguments are scored only over cases whose tool was chosen correctly, so one mistake is not",
        "counted twice. Latency is a gate rather than a term in the score: past ten seconds a model is",
        "disqualified whatever its accuracy.",
        "",
        "**Restraint is measured but weighted lightly (5 %), because the product no longer depends on it.**",
        'The best candidate still queries the database to answer "Hello" one time in three, and the score',
        "moved with the prompt rather than being a stable property of the model. So restraint is decided in",
        "code instead (`agent.conversational_reply`): a greeting is trivially recognisable, and there is no",
        'version of "hello" whose correct answer involves the world model. Delegating that judgement meant',
        "accepting a one-in-three chance of an absurd answer to the easiest possible question, on the axis",
        "where being wrong most damages trust.",
        "",
        f"Fixture: {fixture_size} questions from `services/copilot/src/sio_copilot/evalset.py`, drawn from",
        "UC1-UC5 plus three restraint cases.",
        "",
        "## Results",
        "",
        "| model | selection | arguments | restraint | p50 | p95 | overall | usable |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for score in ranked:
        if score.error:
            lines.append(f"| `{score.model}` | — | — | — | — | — | — | {score.error} |")
            continue
        lines.append(
            f"| `{score.model}` | {score.selection:.0%} | {score.arguments:.0%} | "
            f"{score.restraint:.0%} | {score.p50_ms:,.0f} ms | {score.p95_ms:,.0f} ms | "
            f"{score.overall:.3f} | {'yes' if score.usable else 'no'} |"
        )

    lines += ["", "## Decision", ""]
    if winner is None:
        lines.append("No candidate was scored. Ollama was unreachable.")
    elif winner.usable:
        lines += [
            f"**`{winner.model}` is pinned** in `.env.example` as `SIO_LLM_MODEL`.",
            "",
            f"It selects the right tool for {winner.selection:.0%} of the action cases, fills arguments",
            f"correctly {winner.arguments:.0%} of the time, and stays quiet on {winner.restraint:.0%} of the",
            f"restraint cases, with a p95 of {winner.p95_ms:,.0f} ms.",
        ]
    else:
        lines += [
            "**No candidate clears the bar** (>= 90 % selection, p95 <= 10 s). The best available is",
            f"`{winner.model}` at {winner.selection:.0%} selection and a p95 of {winner.p95_ms:,.0f} ms, and it",
            "is pinned as the least-bad option.",
            "",
            "This is why the copilot ships with a deterministic keyword-router fallback and a scripted",
            "provider: on this class of hardware, tool selection cannot be assumed, so the product does not",
            "depend on it. Every fallback is logged, counted, and stated in the answer's explanation.",
        ]
    lines += [
        "",
        "An exact tag is pinned, never `:latest`. A floating tag means the model can change under a",
        "deployment without anything in the repository changing, and the first symptom is a copilot that",
        "has started choosing the wrong tool.",
        "",
        "## Per-case detail for the pinned model",
        "",
    ]
    if winner and winner.outcomes:
        lines += ["| case | expected | chose | ok | args | ms |", "|---|---|---|---|---|---|"]
        for outcome in winner.outcomes:
            lines.append(
                f"| `{outcome.case_id}` | {outcome.expected or '(none)'} | {outcome.chose or '(none)'} | "
                f"{'yes' if outcome.selection_ok else 'NO'} | "
                f"{'yes' if outcome.arguments_ok else 'no'} | {outcome.latency_ms:,.0f} |"
            )
        failures = [outcome for outcome in winner.outcomes if not outcome.selection_ok]
        if failures:
            lines += ["", "### Where it fails", ""]
            for outcome in failures:
                lines.append(f"- `{outcome.case_id}`: {outcome.detail or 'wrong tool'}")
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models", nargs="*", default=None, help="tags to score (default: all candidates)"
    )
    parser.add_argument("--write-docs", action="store_true", help="update docs/MODELS.md")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    arguments = parser.parse_args()

    belt = ToolBelt(
        api_url="http://127.0.0.1:8000",
        spatial_url="http://127.0.0.1:8106",
        prediction_url="http://127.0.0.1:8108",
        worldmodel_url="http://127.0.0.1:8105",
        ingest_url="http://127.0.0.1:8101",
    )
    specs = belt.specs()

    installed: set[str] = set()
    probe = OllamaLLM()
    if await probe.ping():
        installed = set(await probe.available_models())
    await probe.close()

    wanted = arguments.models or [
        model for model in CANDIDATES if model in installed or not installed
    ]
    if not wanted:
        print("no candidate models installed; try: ollama pull qwen3:1.7b", file=sys.stderr)
        return 1

    print(f"scoring {len(wanted)} model(s) on {len(EVAL_CASES)} cases with {len(specs)} tools\n")
    scores: list[ModelScore] = []
    for model in wanted:
        print(f"  {model} ...", end="", flush=True)
        score = await score_model(model, specs)
        scores.append(score)
        if score.error:
            print(f" {score.error}")
        else:
            print(
                f" selection {score.selection:.0%}  arguments {score.arguments:.0%}  "
                f"restraint {score.restraint:.0%}  p50 {score.p50_ms:,.0f} ms  p95 {score.p95_ms:,.0f} ms"
            )

    ranked = sorted(scores, key=lambda score: -score.overall)
    print("\nranking:")
    for position, score in enumerate(ranked, start=1):
        flag = "USABLE" if score.usable else "below bar"
        print(f"  {position}. {score.model:16} overall {score.overall:.3f}  ({flag})")

    if arguments.json:
        print(json.dumps([score.row() for score in ranked], indent=2))

    if arguments.write_docs:
        target = REPO_ROOT / "docs" / "MODELS.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_docs(scores, fixture_size=len(EVAL_CASES)))
        print(f"\nwrote {target.relative_to(REPO_ROOT)}")

    await belt.close()
    best = ranked[0] if ranked else None
    return 0 if best and best.usable else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
