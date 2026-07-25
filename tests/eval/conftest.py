"""The evaluation harness (PRD §16, Phase 8).

`just eval` runs this ring and prints a scorecard. It is deliberately **not** part of `just check`, and the
distinction is the whole design of this directory.

**Why eval reports instead of failing.** A quality metric wired as a pass/fail gate gets disabled. Detection mAP
moves when the fixture changes, HOTA moves when the tracker's parameters are tuned, and copilot accuracy moves
when the model is swapped — so a build that goes red on a 0.01 drop teaches its team to add `--no-verify`, and
within a month nobody is looking at the numbers at all. The scorecard's job is to be *read*.

**What it does still fail on: a floor.** Every metric declares one, set well below where it currently sits, and
breaching it is a real regression rather than noise — a detector that stops detecting, a tracker that stops
holding identity, a copilot that stops calling tools. The gap between the score and the floor is the headroom,
and the scorecard prints it so a metric drifting toward its floor is visible before it crosses.

**Why the scorecard is a terminal summary rather than a report file.** The number nobody looks at is the one in
`eval-results.json`. Printing a table at the end of the run puts it in front of whoever ran it, which is the
only reliable way a quality metric gets noticed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class Score:
    """One measured metric, with the floor it must not cross."""

    name: str
    value: float
    floor: float
    unit: str = ""
    #: What was measured, in a phrase — printed beside the number because a metric without its subject is a
    #: number somebody will compare against a different one next quarter.
    detail: str = ""
    #: True when the metric could not be measured at all (missing weights, no model running). Distinguished
    #: from a low score, because "we did not measure this" and "this scored badly" call for opposite actions.
    skipped: bool = False
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.skipped or self.value >= self.floor

    @property
    def headroom(self) -> float:
        return self.value - self.floor


@dataclass
class Scorecard:
    """Everything measured in one run."""

    scores: list[Score] = field(default_factory=list)

    def record(
        self,
        name: str,
        value: float,
        *,
        floor: float,
        unit: str = "",
        detail: str = "",
    ) -> Score:
        score = Score(name=name, value=value, floor=floor, unit=unit, detail=detail)
        self.scores.append(score)
        return score

    def skip(self, name: str, *, floor: float, reason: str, unit: str = "") -> Score:
        """Record a metric that could not be measured.

        Recorded rather than omitted, because a scorecard that silently shrinks when the weights are missing is
        one that reports "all green" on a run that measured nothing.
        """
        score = Score(name=name, value=0.0, floor=floor, unit=unit, skipped=True, reason=reason)
        self.scores.append(score)
        return score


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_SCORECARD] = Scorecard()


_SCORECARD = pytest.StashKey[Scorecard]()


@pytest.fixture(scope="session")
def scorecard(request: pytest.FixtureRequest) -> Scorecard:
    return request.config.stash[_SCORECARD]


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    """Print the scorecard.

    Written by hand rather than with a table library: this runs in a terminal summary hook where a missing
    optional dependency would swallow the whole report, and the harness's own output is the last thing that
    should be fragile.
    """
    card = config.stash.get(_SCORECARD, None)
    if card is None or not card.scores:
        return

    width = max(len(score.name) for score in card.scores)
    lines = [
        "",
        "  SIO evaluation scorecard",
        "  " + "─" * (width + 46),
        f"  {'metric':<{width}}  {'score':>9}  {'floor':>8}  {'headroom':>9}  ",
        "  " + "─" * (width + 46),
    ]

    for score in card.scores:
        if score.skipped:
            lines.append(
                f"  {score.name:<{width}}  {'—':>9}  {score.floor:>8.3f}  {'not run':>9}  "
                f"{score.reason}"
            )
            continue
        mark = "ok " if score.passed else "LOW"
        lines.append(
            f"  {score.name:<{width}}  {score.value:>9.3f}  {score.floor:>8.3f}  "
            f"{score.headroom:>+9.3f}  {mark} {score.detail}"
        )

    measured = [score for score in card.scores if not score.skipped]
    breached = [score for score in measured if not score.passed]
    unmeasured = [score for score in card.scores if score.skipped]

    lines.append("  " + "─" * (width + 46))
    lines.append(
        f"  {len(measured)} measured, {len(breached)} below floor, {len(unmeasured)} not run"
    )
    if breached:
        lines.append("")
        lines.append("  Below floor — these are regressions, not noise:")
        lines.extend(
            f"    {score.name}: {score.value:.3f} < {score.floor:.3f}" for score in breached
        )
    if unmeasured:
        lines.append("")
        # Named individually. "3 not run" invites the reader to assume they are the boring ones.
        lines.append("  Not measured this run:")
        lines.extend(f"    {score.name}: {score.reason}" for score in unmeasured)
    lines.append("")

    terminalreporter.write_line("\n".join(lines))


__all__ = ["Score", "Scorecard"]
