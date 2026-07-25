#!/usr/bin/env python
"""End-to-end performance benchmark (PRD §13 NFR, §16 KPI, Phase 8).

Drives synthetic observations into the live platform at a rising rate and measures what an operator would
actually feel: how long until something the platform *concluded* comes back.

    just bench                    # 10 → 50 events/s, the plan's range
    just bench --rates 10,25,50   # explicit
    just bench --seconds 20       # longer, for a steadier number

**Time-to-first-insight is the headline, not throughput.** A platform that ingests 50k messages a second and
takes nine seconds to notice a fire is worse than one that ingests 500 and notices in one. The number that
matters is the interval between an observation entering the bus and a *derived* message — an event, an alert —
coming out with the same `trace_id`. The PRD's target is under 2 seconds and that is what this reports.

**Measured by trace id, not by timestamp arithmetic.** Every message carries the `trace_id` of the observation
it descends from, so the latency is measured on the actual causal chain rather than by correlating two clocks.
It also means a derived message that came from a *different* observation cannot be miscounted as a fast
response to this one — which is the flattering mistake a timestamp-window approach makes under load, precisely
when the number matters most.

**Consumer lag is reported alongside**, because latency alone hides the failure mode that matters at rate: a
pipeline can report good latency for the messages it *finishes* while falling further behind on the ones it has
not started. Lag rising across the rate steps is the signal that the platform is past its capacity, and it
shows up long before latency does.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libs" / "sio_core" / "src"))

DEFAULT_RATES = (10, 25, 50)
DEFAULT_SECONDS = 12.0

#: The PRD's target for time-to-first-insight.
TTFI_TARGET_S = 2.0

#: Topics a derived conclusion arrives on. Raw topics are excluded deliberately: echoing our own observation
#: back would measure the bus's round trip and call it insight.
INSIGHT_TOPICS = ("events", "alerts")


@dataclass
class RateResult:
    """What one rate step measured."""

    rate_hz: int
    published: int
    insights: int
    latencies_ms: list[float] = field(default_factory=list)
    unmatched: int = 0
    """Published observations still in flight when the run was cut off.

    Reported because without it `conversion` is ambiguous: a low number could mean the platform stayed quiet
    (correct) or that the benchmark stopped listening too early (an artefact). Separating them is the difference
    between a result and a rumour — one sweep read 21%, 60%, 50% across rising rates, which is non-monotonic and
    therefore obviously an artefact, but only obvious because I happened to look.
    """
    lag_start: int = 0
    lag_end: int = 0
    cpu_percent: float = 0.0
    rss_mb: float = 0.0
    achieved_hz: float = 0.0

    def percentile(self, fraction: float) -> float:
        if not self.latencies_ms:
            return float("nan")
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, int(fraction * len(ordered)))
        return ordered[index]

    @property
    def p50(self) -> float:
        return self.percentile(0.50)

    @property
    def p95(self) -> float:
        return self.percentile(0.95)

    @property
    def p99(self) -> float:
        return self.percentile(0.99)

    @property
    def conversion(self) -> float:
        """Fraction of published observations that produced a conclusion.

        Not expected to be 1.0 and it should not be: most observations are unremarkable and the platform is
        supposed to stay quiet about them. What matters is that it does not fall to zero as the rate climbs,
        which is what happens when a consumer starts shedding.
        """
        return self.insights / self.published if self.published else 0.0


def _resources() -> tuple[float, float]:
    """CPU percent and resident memory across the platform's processes.

    Best-effort: `psutil` is not a hard dependency, and a benchmark that refuses to run without it would be one
    nobody runs. Reported as zero when unavailable rather than omitted, so the table's shape does not change.
    """
    try:
        import psutil
    except ImportError:
        return 0.0, 0.0

    total_cpu = 0.0
    total_rss = 0.0
    for process in psutil.process_iter(["name", "cmdline"]):
        with contextlib.suppress(Exception):
            cmdline = " ".join(process.info.get("cmdline") or [])
            if "sio_" not in cmdline:
                continue
            total_cpu += process.cpu_percent(interval=None)
            total_rss += process.memory_info().rss / (1024 * 1024)
    return total_cpu, total_rss


async def _drive_rate(bus: Any, rate_hz: int, seconds: float) -> RateResult:
    """Publish at `rate_hz` for `seconds`, listening for what comes back.

    The listener starts BEFORE the publisher, which is not incidental: a consumer group created after the first
    message is published starts at the tail and misses it, so the first observation of every run would look
    like it never produced an insight.
    """
    from sio_schemas import Modality, Observation, Topic, utc_now

    result = RateResult(rate_hz=rate_hz, published=0, insights=0)
    sent_at: dict[str, float] = {}
    group = f"bench-{int(time.time() * 1000)}"
    stop = asyncio.Event()

    async def listen() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            async for message in bus.consume(
                [str(topic) for topic in INSIGHT_TOPICS], group=group, consumer="bench"
            ):
                started = sent_at.pop(message.trace_id, None)
                if started is not None:
                    # The causal chain, by trace id. A timestamp window would count a conclusion derived from
                    # somebody else's observation as a fast response to ours — flattering, and most wrong
                    # exactly when the pipeline is busy.
                    result.latencies_ms.append((time.perf_counter() - started) * 1000)
                    result.insights += 1
                if stop.is_set() and not sent_at:
                    return

    for topic in INSIGHT_TOPICS:
        await bus.ensure_group(str(topic), group)
    listener = asyncio.create_task(listen())

    interval = 1.0 / rate_hz
    deadline = time.perf_counter() + seconds
    next_send = time.perf_counter()
    started_at = time.perf_counter()

    while time.perf_counter() < deadline:
        now = time.perf_counter()
        if now < next_send:
            await asyncio.sleep(min(next_send - now, 0.01))
            continue
        next_send += interval

        observation = Observation(
            # A UNIQUE source per message, and this is the difference between a benchmark and a single
            # sample. The temperature rule's `cooldown_key` is `[source_id]`, so a fixed source id means the
            # first observation fires and the next 119 are suppressed — correct platform behaviour, and it
            # gave a p50, p95 and p99 all computed from ONE measurement. A percentile from one sample is not
            # a percentile.
            #
            # Distinct sources are also the more honest load: a real site has many sensors reporting, not one
            # sensor reporting repeatedly, and the fan-out through the rule engine's cooldown bookkeeping is
            # part of what is being measured.
            source_id=f"bench-{result.published:06d}",
            modality=Modality.IOT,
            ts=utc_now(),
            payload={
                "metric": "temperature_c",
                # Above the temperature rule's 60°C threshold, so this reliably produces a conclusion. A
                # benchmark whose input never triggers anything measures the bus, not the platform.
                "value": 82.0,
                "bench": True,
            },
        )
        sent_at[observation.trace_id] = time.perf_counter()
        await bus.publish(
            Topic.RAW_IOT, observation, producer="bench", trace_id=observation.trace_id
        )
        result.published += 1

    result.achieved_hz = result.published / max(1e-9, time.perf_counter() - started_at)
    stop.set()

    # A grace period for in-flight work. Without it the last second of publishing is scored as latency
    # infinity, which drags every percentile and makes a short run look worse than a long one.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(listener, timeout=5.0)
    listener.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await listener

    # Whatever is left never came back inside the grace period.
    result.unmatched = len(sent_at)
    result.cpu_percent, result.rss_mb = _resources()
    return result


async def _lag(bus: Any, group: str = "cg.events") -> int:
    """How far behind a real consumer group is.

    `cg.events` rather than our own group: the question is whether the PLATFORM is keeping up, and the
    benchmark's own consumer is not part of the platform.
    """
    from sio_schemas import Topic

    with contextlib.suppress(Exception):
        return int(await bus.lag(str(Topic.RAW_IOT), group))
    return 0


async def run(rates: tuple[int, ...], seconds: float) -> list[RateResult]:
    from sio_core import get_bus, get_settings

    settings = get_settings()
    bus = get_bus(settings)
    if not await bus.ping():
        raise SystemExit(
            "the bus is unreachable. Start the platform first: `just services && just dev`."
        )

    results: list[RateResult] = []
    for rate in rates:
        lag_start = await _lag(bus)
        print(f"  driving {rate} events/s for {seconds:.0f}s…", flush=True)
        result = await _drive_rate(bus, rate, seconds)
        result.lag_start = lag_start
        result.lag_end = await _lag(bus)
        results.append(result)
        # A pause between steps so the previous rate's backlog drains, or step two measures step one's queue
        # and every subsequent number is worse than the truth.
        await asyncio.sleep(3.0)

    with contextlib.suppress(Exception):
        await bus.close()
    return results


def render(results: list[RateResult]) -> str:
    lines = [
        "",
        "  SIO end-to-end benchmark",
        "  " + "─" * 98,
        f"  {'rate':>6}  {'sent':>6}  {'achieved':>9}  {'insights':>9}  "
        f"{'p50 ms':>8}  {'p95 ms':>8}  {'p99 ms':>8}  {'lag':>6}  {'inflight':>8}  {'RSS MB':>7}",
        "  " + "─" * 98,
    ]
    for result in results:
        lines.append(
            f"  {result.rate_hz:>6}  {result.published:>6}  {result.achieved_hz:>8.1f}/s  "
            f"{result.insights:>9}  {result.p50:>8.0f}  {result.p95:>8.0f}  {result.p99:>8.0f}  "
            f"{result.lag_end:>6}  {result.unmatched:>8}  {result.rss_mb:>7.0f}"
        )
    lines.append("  " + "─" * 98)

    measured = [result for result in results if result.latencies_ms]
    if measured:
        best = min(result.p50 for result in measured)
        worst = max(result.p95 for result in measured)
        verdict = "within" if worst / 1000 <= TTFI_TARGET_S else "OVER"
        lines.extend(
            [
                "",
                f"  Time to first insight: p50 {best / 1000:.2f}s at the lightest rate, "
                f"p95 {worst / 1000:.2f}s at the heaviest.",
                f"  PRD target is {TTFI_TARGET_S:.0f}s — {verdict} target.",
                "",
                "  What to read here: p95 rising across the rate steps means the pipeline is at its limit;",
                "  LAG rising while latency stays flat means it is already past it and the good latency is",
                "  only for the messages it has managed to finish.",
            ]
        )
        # Conversion is reported rather than hidden, with the caveat, because a reader will otherwise assume
        # one observation should equal one insight.
        conversions = ", ".join(
            f"{result.rate_hz}/s: {result.conversion:.0%}" for result in results
        )
        lines.append(
            f"  Conversion (observations that produced a conclusion) — {conversions}. Not expected to be"
        )
        lines.append(
            "  100%: cooldowns suppress repeats on purpose, and a platform that alerted on every reading"
        )
        lines.append(
            "  would be one nobody reads. It falling toward zero as rate climbs is the warning."
        )
        inflight = sum(result.unmatched for result in results)
        if inflight:
            lines.append(
                f"  Read conversion together with INFLIGHT ({inflight} total): those were still in the"
            )
            lines.append(
                "  pipeline at the cutoff, so they depress conversion without saying anything about the"
            )
            lines.append("  platform. A non-monotonic conversion column is that, not a finding.")
    else:
        lines.extend(
            [
                "",
                "  No insights were observed. That is a result, not an error: it means observations went in",
                "  and nothing came out on events/alerts. Check that the events service is running and that",
                "  its rules match the benchmark's payload (temperature_c at 82).",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rates",
        default=",".join(str(rate) for rate in DEFAULT_RATES),
        help="comma-separated events per second (default: 10,25,50)",
    )
    parser.add_argument(
        "--seconds", type=float, default=DEFAULT_SECONDS, help="seconds per rate step"
    )
    arguments = parser.parse_args()

    rates = tuple(int(value) for value in arguments.rates.split(",") if value.strip())
    print(f"Driving {rates} events/s for {arguments.seconds:.0f}s each.")
    print("Time to first insight is measured by trace id, on the causal chain.\n")

    results = asyncio.run(run(rates, arguments.seconds))
    print(render(results))

    measured = [result for result in results if result.latencies_ms]
    if not measured:
        return 1
    # Non-zero when the target is missed, so this can gate a release if somebody wants it to — but it is not
    # in `just check`, because a latency assertion on shared CI hardware fails for reasons that have nothing
    # to do with the code.
    worst_p95 = max(result.p95 for result in measured) / 1000
    return 0 if worst_p95 <= TTFI_TARGET_S else 2


if __name__ == "__main__":
    raise SystemExit(main())
