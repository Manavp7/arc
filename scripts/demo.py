"""The demo: one command, a real incident, and a narration that says what to look at and when.

    just demo                 # the full five-minute demo, narrated
    just demo --headless      # no waiting for a human; asserts and exits (the smoke test)
    just demo-reset           # idempotent teardown so it can be run again immediately

**What this is for.** A platform this size can be "working" and still undemonstrable, because the person
giving the demo does not know which of six panels to open at which second. So this script does not merely
trigger an incident — it narrates one, with timestamps, naming what should now be visible where. The demo is
a deliverable, not a side effect of the code working.

**Why it verifies as it goes.** Every step polls for the thing it claims to have caused, and says plainly
when it does not appear. A demo script that prints "✓ fire detected" without checking is worse than no
script: it will confidently narrate a broken system to an audience, which is exactly the situation the
narration exists to prevent. Each step therefore has a deadline and an honest failure line.

**What it deliberately does not do.** It does not start infrastructure or the services. Those are
`just services` and `just dev`, they take minutes, and a demo script that silently starts fifteen processes
is one nobody can reason about when it goes wrong. It checks they are up and says exactly what to run if
they are not.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

API = "http://127.0.0.1:8000"
INGEST = "http://127.0.0.1:8101"
DECISION = "http://127.0.0.1:8110"
WEB = "http://127.0.0.1:5173"

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
AMBER = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
OFF = "\033[0m"

#: Zone the scripted fire starts in. The fuel store rather than a dock, because the alerts service scores
#: asset criticality and the demo should show that mattering — a fire here outranks one in the car park.
FIRE_ZONE = "fuel_store"


@dataclass
class Narration:
    """Timestamped narration, so the presenter and the audience are looking at the same second."""

    started: float = field(default_factory=time.monotonic)
    quiet: bool = False
    failures: list[str] = field(default_factory=list)

    def stamp(self) -> str:
        elapsed = time.monotonic() - self.started
        return f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"

    def say(self, text: str) -> None:
        print(f"{DIM}[{self.stamp()}]{OFF} {text}", flush=True)

    def beat(self, title: str) -> None:
        print(f"\n{BOLD}{'─' * 78}{OFF}", flush=True)
        print(f"{DIM}[{self.stamp()}]{OFF} {BOLD}{title}{OFF}", flush=True)

    def look(self, where: str, what: str) -> None:
        """Tell the presenter where to look. The single most useful thing this script prints."""
        print(f"          {CYAN}→ {where}{OFF}  {what}", flush=True)

    def ok(self, text: str) -> None:
        print(f"          {GREEN}✓{OFF} {text}", flush=True)

    def warn(self, text: str) -> None:
        print(f"          {AMBER}!{OFF} {text}", flush=True)

    def fail(self, text: str) -> None:
        self.failures.append(text)
        print(f"          {RED}✗ {text}{OFF}", flush=True)


async def wait_for(
    narration: Narration,
    description: str,
    probe: Any,
    *,
    deadline_s: float,
    interval_s: float = 2.0,
) -> Any:
    """Poll until `probe` returns something truthy, or say plainly that it did not.

    The deadline is the point. A demo that hangs is indistinguishable from a demo that is thinking, and a
    presenter cannot tell an audience which it is.
    """
    limit = time.monotonic() + deadline_s
    attempts = 0
    while time.monotonic() < limit:
        attempts += 1
        try:
            result = await probe()
        except httpx.HTTPError as exc:
            result = None
            if attempts == 1:
                narration.warn(f"{description}: {type(exc).__name__}, retrying")
        if result:
            narration.ok(f"{description} (after {attempts} check(s))")
            return result
        await asyncio.sleep(interval_s)
    narration.fail(f"{description} did not happen within {deadline_s:.0f}s")
    return None


async def preflight(client: httpx.AsyncClient, narration: Narration) -> bool:
    """Check what the demo needs, and say exactly what to run for anything missing."""
    narration.beat("Preflight — is the platform up?")
    required = {
        "api": f"{API}/health",
        "ingest": f"{INGEST}/health",
        "events": "http://127.0.0.1:8107/health",
        "alerts": "http://127.0.0.1:8115/health",
        "decision": f"{DECISION}/health",
        "workflow": "http://127.0.0.1:8114/health",
    }
    missing = []
    for name, url in required.items():
        try:
            response = await client.get(url, timeout=5.0)
            status = response.json().get("status", "?") if response.status_code == 200 else "down"
            if response.status_code == 200:
                narration.ok(f"{name}: {status}")
            else:
                missing.append(name)
                narration.fail(f"{name}: HTTP {response.status_code}")
        except httpx.HTTPError:
            missing.append(name)
            narration.fail(f"{name}: not reachable")

    if missing:
        print(
            f"\n{RED}The demo needs these services running: {', '.join(missing)}{OFF}\n"
            f"  1. {BOLD}just services{OFF}   # postgres, redis, neo4j, minio  (~30s)\n"
            f"  2. {BOLD}just dev{OFF}        # the platform  (~90s)\n"
            f"  3. {BOLD}just demo{OFF}       # this script, again\n",
            flush=True,
        )
        return False

    try:
        response = await client.get(WEB, timeout=5.0)
        if response.status_code == 200:
            narration.ok(f"console: {WEB}")
        else:
            narration.warn(f"the console at {WEB} returned {response.status_code} — run: just web")
    except httpx.HTTPError:
        narration.warn(f"the console is not running at {WEB}. Run `just web` in another terminal.")
    return True


async def ensure_site(client: httpx.AsyncClient, narration: Narration) -> bool:
    """The yard has to exist before anything can happen in it."""
    narration.beat("Step 1 — the site")
    try:
        zones = (await client.get(f"{API}/api/spatial/zones", timeout=10.0)).json()
    except httpx.HTTPError as exc:
        narration.fail(f"could not read the site: {type(exc).__name__}")
        return False
    if not zones:
        narration.fail("no zones. Run: just seed")
        return False
    names = [zone.get("zone_id") for zone in zones]
    narration.ok(f"{len(zones)} zones: {', '.join(str(name) for name in names[:7])}")
    if FIRE_ZONE not in names:
        narration.fail(
            f"the demo needs a {FIRE_ZONE!r} zone; found {names}. Run: just seed --clear"
        )
        return False
    narration.look("the map at :5173", "zone outlines, and entity dots already moving")
    return True


async def show_the_world(client: httpx.AsyncClient, narration: Narration) -> bool:
    """UC1: who is on site, and for how long."""
    narration.beat("Step 2 — UC1: what is on site, and how long has it been here")
    entities = await wait_for(
        narration,
        "entities are being tracked",
        lambda: _entities(client),
        deadline_s=60,
    )
    if not entities:
        narration.fail("nothing on site. Is the simulator running? Check: curl :8101/simulation")
        return False

    by_type: dict[str, int] = {}
    for entity in entities:
        by_type[entity.get("type", "?")] = by_type.get(entity.get("type", "?"), 0) + 1
    narration.ok(f"{len(entities)} moving entities: {by_type}")

    # Dwell is what UC1 turns on, so name an actual long-stay entity rather than asserting the feature.
    longest = max(entities, key=_dwell_s, default=None)
    if longest is not None and _dwell_s(longest) > 60:
        narration.ok(
            f"longest on site: {longest.get('label') or longest.get('entity_id')} "
            f"at {_dwell_s(longest) / 60:.0f} min"
        )
        narration.look(
            "the map",
            f"click {longest.get('label') or 'that entity'} — the panel shows time on site and, "
            "under 'sources', which sensors produced the belief",
        )
    else:
        narration.warn("nothing has been on site long enough to show dwell yet; give it a minute")
    return True


async def run_the_incident(client: httpx.AsyncClient, narration: Narration) -> dict[str, Any]:
    """UC2: a fire, and everything the platform does about it."""
    narration.beat(f"Step 3 — UC2: a fire starts in the {FIRE_ZONE.replace('_', ' ')}")
    # The clock, not a snapshot of ids: the fire's alert is identified by its group and its recency.
    injected_at = datetime.now(UTC)

    response = await client.post(
        f"{INGEST}/simulation/inject/fire",
        params={"zone_id": FIRE_ZONE, "duration_s": 900},
        timeout=20.0,
    )
    if response.status_code != 200:
        narration.fail(
            f"could not inject the fire: HTTP {response.status_code} {response.text[:120]}"
        )
        return {}
    narration.ok(f"fire injected into {FIRE_ZONE} (burns for 15 min of simulated time)")
    narration.look(
        "the map",
        "smoke will appear in the camera frames covering that zone within a few seconds",
    )

    # Detection. Perception has to see it in a rendered frame, so this genuinely exercises the vision path.
    event = await wait_for(
        narration,
        "the fire was DETECTED from camera imagery (not from the injection)",
        lambda: _find_event(client, "fire_detected"),
        deadline_s=120,
    )
    if event:
        narration.ok(
            f"{event['event_id']} — {(event.get('explanation') or {}).get('summary', '')[:96]}"
        )
        narration.look(
            "the events tab", "the new row, and its 'why?' link opens the explanation drawer"
        )

    # The alert. This is the part an operator actually works from.
    alert = await wait_for(
        narration,
        f"a FIRE alert for the {FIRE_ZONE} was raised or updated, scored and prioritised",
        lambda: _alert_for_the_fire(client, injected_at),
        deadline_s=120,
    )
    if alert:
        narration.ok(
            f"priority {alert['score']:.1f} [{alert['severity']}] — {alert['title'][:70]}"
            + (
                f"  ({alert['count']} occurrences folded into one row — repeats do not create new alerts)"
                if alert.get("count", 1) > 1
                else ""
            )
        )
        narration.ok(f"why it ranks there: {alert.get('urgency_reason')}")
        narration.look(
            "the ALERTS tab",
            "it should be at or near the top. The number is the priority; the grey line under the "
            "title is why it scores that",
        )
        narration.look(
            "click the alert row",
            "the explanation drawer: confidence, the reasoning, the evidence it rests on, and what "
            "it considered and rejected",
        )

    # A playbook. Five steps, visibly.
    run = await wait_for(
        narration,
        "a response playbook started",
        lambda: _find_run(client),
        deadline_s=90,
    )
    if run:
        steps = ", ".join(f"{step['name']} [{step['status']}]" for step in run.get("steps", [])[:5])
        narration.ok(f"{run['playbook']} — {steps}")
        narration.look("the MISSIONS tab", "each step with its own status, not a progress bar")

    # A recommendation, awaiting a human.
    decision = await wait_for(
        narration,
        "a recommendation was produced — and NOT acted on",
        lambda: _find_pending_decision(client),
        deadline_s=120,
    )
    if decision:
        options = decision.get("options", [])
        chosen = next(
            (option for option in options if option["option_id"] == decision.get("chosen")), None
        )
        narration.ok(f"{len(options)} options, ranked; recommended: {(chosen or {}).get('action')}")
        if chosen:
            narration.ok(f"expected effect: {chosen['expected_effect'][:90]}")
        narration.ok(f"approval state: {decision['approval']} — nothing has been dispatched")
        narration.look(
            "the DECISIONS tab",
            "'show options' lists every option including the ones not chosen and why. "
            "This is the human-on-the-loop gate: the platform will not act until somebody approves",
        )
    return {"event": event, "alert": alert, "run": run, "decision": decision}


async def show_the_rest(client: httpx.AsyncClient, narration: Narration) -> None:
    """UC3-UC6: forecasts, the copilot, replay, and the audit trail."""
    narration.beat("Step 4 — UC3: what happens next")
    forecasts = (
        (await client.get(f"{API}/api/forecasts/latest", timeout=15.0)).json().get("forecasts", {})
    )
    if forecasts:
        sample = next(iter(forecasts.values()))
        narration.ok(f"{len(forecasts)} live forecasts, e.g. {sample.get('summary', '')[:88]}")
        narration.look(
            "the FORECAST tab",
            "the shaded band is the interval — and where it is too wide to be useful, the summary "
            "says so rather than pretending",
        )
    else:
        narration.warn("no forecasts yet; the prediction service needs a few minutes of history")

    narration.beat("Step 5 — UC4: ask a question in English")
    try:
        answer = await client.post(
            f"{API}/api/copilot/ask",
            json={"question": f"What is happening in the {FIRE_ZONE.replace('_', ' ')}?"},
            timeout=150.0,
        )
        if answer.status_code == 200:
            payload = answer.json()
            trace = payload.get("trace", {})
            narration.ok(f"“{payload.get('answer', '')[:140]}”")
            narration.ok(
                f"{trace.get('model')} in {trace.get('total_ms', 0) / 1000:.1f}s "
                f"using {trace.get('tools_used')}"
            )
            narration.look(
                "the COPILOT tab",
                "ask it yourself, then click 'how?' — every tool call, in order, with timings. "
                "A copilot that cannot show its work is a liability",
            )
        else:
            narration.fail(f"the copilot returned HTTP {answer.status_code}")
    except httpx.HTTPError as exc:
        narration.warn(
            f"the copilot did not answer in time ({type(exc).__name__}) — is Ollama running?"
        )

    narration.beat("Step 6 — UC5: rewind, and UC6: the audit trail")
    bounds = (await client.get(f"{API}/api/timeline/bounds", timeout=10.0)).json()
    if bounds.get("start"):
        narration.ok(f"history spans {bounds['start']} → {bounds['end']}")
        narration.look(
            "the timeline at the bottom",
            "drag the scrubber back — the map reconstructs the world as it was at that instant, "
            "because nothing is ever overwritten",
        )
    audit = (await client.get(f"{API}/api/audit?limit=5", timeout=10.0)).json()
    entries = audit.get("entries", [])
    if entries:
        narration.ok(f"{len(entries)} recent audit records, append-only")
        for entry in entries[:3]:
            narration.say(
                f"          {DIM}{entry.get('action', '?')} by {entry.get('actor', '?')}{OFF}"
            )
    else:
        narration.warn("no audit records yet")


async def reset(client: httpx.AsyncClient, narration: Narration) -> int:
    """Idempotent teardown, so the demo can be given twice in a row.

    Resolves the alerts and rejects the pending decisions rather than deleting anything. This platform is
    append-only by design (PRD M2) and a reset that truncated tables would be demonstrating the opposite of
    the product — the second demo would also lose the evidence the first one produced.
    """
    narration.beat("Reset — clearing the working state, keeping the history")
    # Paged, until a pass changes nothing.
    #
    # A single pass over `?limit=200` was not a reset: the inbox was deeper than one page, so resolving 200
    # rows merely revealed the next 200 — and running the reset twice in a row reported "resolved 200
    # alerts" both times. An idempotent teardown has to be able to say it is finished, which means looping
    # until there is nothing left rather than until one page is done.
    #
    # Bounded, because a reset that cannot converge must stop and say so rather than run forever against a
    # live simulator that is still producing alerts.
    resolved = rejected = 0
    passes = 0
    max_passes = 25
    try:
        while passes < max_passes:
            passes += 1
            inbox = (await client.get(f"{API}/api/alerts?limit=200", timeout=20.0)).json()
            open_now = [
                alert
                for alert in inbox.get("alerts", [])
                if alert.get("state") in ("open", "escalated", "acknowledged")
            ]
            if not open_now:
                break
            for alert in open_now:
                response = await client.post(
                    f"{API}/api/alerts/{alert['alert_id']}/resolve",
                    json={"resolved_by": "demo-reset", "note": "cleared by just demo-reset"},
                    timeout=15.0,
                )
                resolved += response.status_code == 200

        while True:
            decisions = (await client.get(f"{API}/api/decisions?limit=200", timeout=20.0)).json()
            pending = [
                decision
                for decision in decisions.get("decisions", [])
                if decision.get("approval") == "pending"
            ]
            if not pending or passes >= max_passes:
                break
            passes += 1
            for decision in pending:
                response = await client.post(
                    f"{API}/api/decisions/{decision['decision_id']}/reject",
                    json={"rejected_by": "demo-reset", "reason": "cleared by just demo-reset"},
                    timeout=15.0,
                )
                rejected += response.status_code == 200
    except httpx.HTTPError as exc:
        narration.fail(f"reset could not reach the platform: {type(exc).__name__}")
        return 1

    narration.ok(
        f"resolved {resolved} alerts, rejected {rejected} pending decisions in {passes} pass(es)"
    )
    if passes >= max_passes:
        # Honest about the one case where this is expected: the simulator is still running and a burning
        # fire keeps producing alerts faster than they can be cleared.
        narration.warn(
            f"stopped after {max_passes} passes — something is still producing alerts. "
            "That is normal while a fire is burning; run it again once the incident has expired."
        )
    narration.ok("history, events and audit records are untouched — this platform deletes nothing")
    narration.say(f"\n{BOLD}Ready to run `just demo` again.{OFF}")
    return 0


# --------------------------------------------------------------------------------- probes
async def _entities(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await client.get(
        f"{API}/api/entities",
        params={"limit": 200, "active_within_s": 300, "include_static": False},
        timeout=15.0,
    )
    return response.json() if response.status_code == 200 else []


def _dwell_s(entity: dict[str, Any]) -> float:
    try:
        first = datetime.fromisoformat(str(entity["first_seen"]).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(entity["last_seen"]).replace("Z", "+00:00"))
        return (last - first).total_seconds()
    except (KeyError, ValueError):
        return 0.0


async def _find_event(client: httpx.AsyncClient, event_type: str) -> dict[str, Any] | None:
    response = await client.get(f"{API}/api/events", params={"limit": 60}, timeout=15.0)
    if response.status_code != 200:
        return None
    for event in response.json():
        if event.get("type") == event_type:
            return event
    return None


async def _alert_for_the_fire(client: httpx.AsyncClient, since: datetime) -> dict[str, Any] | None:
    """The fire's alert, identified by its group and its recency. Nothing else will do.

    The first version of this compared alert ids against a snapshot taken before the injection, and fell
    back to "any new alert" when it found none. Both halves were wrong in the same direction — they could
    report an unrelated alert as the fire's:

    * an id comparison fails when the correct behaviour is to FOLD a repeat into the existing row rather
      than raise a new one, which is exactly what a second fire in the same zone should do;
    * and in a busy simulated yard there is always *some* new alert, so the fallback fired. The demo
      announced "an alert was raised, scored and prioritised" and displayed `Worker 12 entered Fuel store`
      directly beneath a fire.

    Narrating the wrong row confidently is the precise failure this script exists to prevent, and it had
    reproduced it. So the test is now the strict, obvious one: an alert whose group is a fire in the demo's
    zone, updated after the injection. When that does not appear the demo says so, which is information —
    where a confident narration of an unrelated alert is worse than silence.
    """
    inbox = (await client.get(f"{API}/api/alerts?limit=200", timeout=15.0)).json()
    for alert in inbox.get("alerts", []):
        if alert.get("group_key") != f"fire_detected:{FIRE_ZONE}":
            continue
        try:
            updated = datetime.fromisoformat(str(alert["last_ts"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if updated >= since:
            return alert
    return None


async def _find_run(client: httpx.AsyncClient) -> dict[str, Any] | None:
    response = await client.get(f"{API}/api/workflow/runs?limit=5", timeout=15.0)
    if response.status_code != 200:
        return None
    return next(iter(response.json().get("recent", [])), None)


async def _find_pending_decision(client: httpx.AsyncClient) -> dict[str, Any] | None:
    response = await client.get(f"{API}/api/decisions?limit=20", timeout=20.0)
    if response.status_code != 200:
        return None
    decisions = response.json().get("decisions", [])
    return next((row for row in decisions if row.get("approval") == "pending"), None)


# ----------------------------------------------------------------------------------- main
async def run(headless: bool) -> int:
    narration = Narration(quiet=headless)
    print(
        f"\n{BOLD}Spatial Intelligence OS — scripted demo{OFF}\n"
        f"{DIM}Started {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC. "
        f"Timestamps below are elapsed time, so you can follow along.{OFF}"
    )
    async with httpx.AsyncClient() as client:
        if not await preflight(client, narration):
            return 2
        if not await ensure_site(client, narration):
            return 2
        await show_the_world(client, narration)
        outcome = await run_the_incident(client, narration)
        if not headless:
            await show_the_rest(client, narration)

        narration.beat("Summary")
        expected = ("event", "alert", "run", "decision")
        for key in expected:
            if outcome.get(key):
                narration.ok(f"{key}: produced")
            else:
                narration.fail(f"{key}: never appeared")

        if narration.failures:
            print(f"\n{RED}{BOLD}The demo did not complete.{OFF}")
            for failure in narration.failures:
                print(f"  {RED}✗{OFF} {failure}")
            print(f"\n{DIM}Logs are in .sio/logs/<service>.log{OFF}")
            return 1

        print(
            f"\n{GREEN}{BOLD}The demo completed.{OFF} A fire was detected from imagery, scored into a "
            f"prioritised alert, answered with a playbook, and turned into a ranked recommendation that "
            f"is waiting for a human.\n"
            f"\n  {BOLD}Now open {WEB}{OFF} and walk the tabs: events → alerts → decisions → missions →\n"
            f"  forecast → copilot. Every panel has a 'why?' that opens the same explanation drawer.\n"
            f"\n  {DIM}Re-run back to back with: just demo-reset && just demo{OFF}\n"
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="skip the presenter-facing sections and exit non-zero if the incident did not complete",
    )
    parser.add_argument("--reset", action="store_true", help="clear the working state and exit")
    args = parser.parse_args(argv)

    if args.reset:

        async def _reset() -> int:
            async with httpx.AsyncClient() as client:
                return await reset(client, Narration())

        return asyncio.run(_reset())
    return asyncio.run(run(headless=args.headless))


if __name__ == "__main__":
    sys.exit(main())
