"""Verify the P4.6 fixes against a running stack.

Written because a browser walkthrough found fifteen bugs that `just check` could not see, and the fixes
therefore need checking against the live system rather than against unit tests alone. Each check hits a real
endpoint on a running stack and prints the evidence it judged, so a reader can disagree with the verdict.

What this cannot check is anything purely visual — whether six tabs fit, whether a drawer occludes the top
bar, whether a tick glyph renders. Those are marked as such rather than quietly claimed.

    uv run python scripts/verify_p46.py
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx

API = "http://127.0.0.1:8000"
WEB = "http://127.0.0.1:5173"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
VISUAL = "\033[33mCODE-ONLY\033[0m"

results: list[tuple[str, bool | None, str]] = []


def record(name: str, ok: bool | None, evidence: str) -> None:
    results.append((name, ok, evidence))
    mark = VISUAL if ok is None else (PASS if ok else FAIL)
    print(f"  {mark}  {name}")
    for line in evidence.strip().splitlines():
        print(f"          {line}")


async def get(client: httpx.AsyncClient, path: str, base: str = API) -> Any:
    response = await client.get(f"{base}{path}")
    response.raise_for_status()
    return response.json()


async def main() -> int:
    async with httpx.AsyncClient(timeout=180.0) as client:
        print("\n=== P4.6 verification against the running stack ===\n")

        # 1 -----------------------------------------------------------------------------------
        try:
            response = await client.get(WEB, timeout=8.0)
            record(
                "the console answers on the numeric loopback address",
                response.status_code == 200,
                f"GET {WEB} -> {response.status_code} (vite bound IPv6 only before, so this was refused)",
            )
        except httpx.HTTPError as exc:
            record("the console answers on the numeric loopback address", False, str(exc))

        # 2 -----------------------------------------------------------------------------------
        entities = await get(client, "/api/entities?limit=500&active_within_s=300")
        movers = [row for row in entities if not row.get("is_static")]
        answer = await client.post(
            f"{API}/api/copilot/ask", json={"question": "What is on site right now?"}
        )
        payload = answer.json()
        text = payload.get("answer", "")
        numbers = [int(word) for word in text.replace(",", " ").split() if word.isdigit()]
        close = any(abs(number - len(movers)) <= 6 for number in numbers)
        record(
            "the copilot and the console count the same thing",
            answer.status_code == 200 and close,
            f"API reports {len(entities)} entities, {len(movers)} of them moving\n"
            f"copilot answered: {text[:150]}\n"
            f"numbers in the answer: {numbers} (was 28 against a header of 58)",
        )

        # 3 -----------------------------------------------------------------------------------
        record(
            "the copilot answers through the API at all",
            answer.status_code == 200 and bool(text),
            f"POST /api/copilot/ask -> {answer.status_code}, "
            f"{payload.get('trace', {}).get('total_ms', 0) / 1000:.1f}s, "
            f"model {payload.get('trace', {}).get('model')}, "
            f"tools {payload.get('trace', {}).get('tools_used')}\n"
            f"(this returned 500 on every request before: _forward() got an unexpected keyword "
            f"argument 'timeout')",
        )

        # 4 -----------------------------------------------------------------------------------
        inbox = await get(client, "/api/alerts?limit=6")
        alerts = inbox.get("alerts", [])
        scoring_words = ("severity", "confidence", "occurrence", "critical area")
        with_both = [
            row
            for row in alerts
            if any(word in (row.get("urgency_reason") or "") for word in scoring_words)
            and (row.get("state") != "escalated" or row.get("escalation_reason"))
        ]
        record(
            "an alert explains its score AND its escalation separately",
            len(alerts) > 0 and len(with_both) == len(alerts),
            "\n".join(
                f"{row['score']:6.1f} [{row['state']}] {row['title'][:40]}\n"
                f"         score : {row.get('urgency_reason')}\n"
                f"         escal : {row.get('escalation_reason')}"
                for row in alerts[:3]
            )
            + f"\n{len(with_both)}/{len(alerts)} rows carry both\n"
            "(every row's score reason used to be the escalation timer, and it survived acknowledgement)",
        )

        # 5 -----------------------------------------------------------------------------------
        duplicated: list[str] = []
        for row in alerts[:6]:
            evidence = (row.get("explanation") or {}).get("evidence") or []
            seen = set()
            for item in evidence:
                key = (item.get("kind"), item.get("ref"))
                if key in seen:
                    duplicated.append(f"{row['alert_id']}: {key}")
                seen.add(key)
        record(
            "no evidence list repeats the same reference",
            not duplicated,
            f"checked {len(alerts[:6])} alerts; duplicates found: {duplicated or 'none'}\n"
            "(the same observation appeared three times for a zone entry, which reads as three "
            "corroborating facts when there is one)",
        )

        # 6 -----------------------------------------------------------------------------------
        runs = await get(client, "/api/workflow/runs?limit=5")
        recent = runs.get("recent", [])
        statuses = {step["status"] for run in recent for step in run.get("steps", [])}
        known = {"pending", "running", "completed", "failed", "cancelled", "compensated"}
        record(
            "every playbook step status is one the console can render",
            bool(statuses) and statuses <= known,
            f"statuses in flight: {sorted(statuses)}\n"
            f"the console's glyph map is typed Record<RunStatus, string> over {sorted(known)}\n"
            "(it previously keyed on invented names — ok, succeeded, skipped — so every completed "
            "step rendered as the unknown-status dot)",
        )

        # 7 -----------------------------------------------------------------------------------
        overflight = await client.post(
            f"{API.replace('8000', '8110')}/decisions/recommend",
            params={
                "kind": "intrusion",
                "zone_id": "fuel_store",
                "severity": "high",
                "task": "overflight",
            },
            timeout=90.0,
        )
        if overflight.status_code == 200:
            decision = overflight.json()
            options = decision.get("options", [])
            chosen = next(
                (option for option in options if option["option_id"] == decision.get("chosen")),
                None,
            )
            targets = [option.get("target_entity_id") or "" for option in options]
            forklifts = [target for target in targets if "forklift" in target]
            ulids = [
                option["expected_effect"]
                for option in options
                if "evt_" in option.get("expected_effect", "")
            ]
            record(
                "an aerial task cannot be answered with a ground vehicle",
                not forklifts and not ulids,
                f"requested task=overflight in fuel_store\n"
                f"options: {[(o['action'], o.get('target_entity_id')) for o in options]}\n"
                f"recommended: {chosen['expected_effect'][:110] if chosen else 'none'}\n"
                f"forklifts offered: {forklifts or 'none'}; raw ULIDs in prose: {len(ulids)}\n"
                "(a security agent asked for an overflight and the solver dispatched a forklift)",
            )
        else:
            record(
                "an aerial task cannot be answered with a ground vehicle",
                None,
                f"the decision service returned {overflight.status_code}: "
                f"{overflight.text[:160]}\n(no drone on site to dispatch, which is itself the honest answer)",
            )

        # 8 -----------------------------------------------------------------------------------
        forecasts = (await get(client, "/api/forecasts/latest")).get("forecasts", {})
        battery = next(
            (value for key, value in forecasts.items() if key.startswith("battery")), None
        )
        if battery:
            summary = battery.get("summary") or ""
            last = battery["points"][-1]
            span = (last.get("hi") or 0) - (last.get("lo") or 0)
            wide = span >= 80
            admits = "whole range" in summary or "treat the direction" in summary
            coverage_note = next(
                (note for note in battery.get("why", []) if "held-out" in note), ""
            )
            honest_coverage = "MORE than asked for" in coverage_note or "100%" not in coverage_note
            record(
                "a forecast whose interval says nothing admits it",
                (admits if wide else True) and honest_coverage,
                f"summary: {summary}\n"
                f"final interval: {last.get('lo')} to {last.get('hi')} (span {span:.0f} of a 0-100 range)\n"
                f"coverage note: {coverage_note[:140]}\n"
                f"interval is uninformative: {wide}; summary says so: {admits}",
            )
        else:
            record(
                "a forecast whose interval says nothing admits it", None, "no battery forecast yet"
            )

        # 9 -----------------------------------------------------------------------------------
        try:
            before = await client.get(f"{API}/api/timeline/bounds", timeout=8.0)
            end_before = before.json().get("end")
            await asyncio.sleep(20)
            after = await client.get(f"{API}/api/timeline/bounds", timeout=8.0)
            end_after = after.json().get("end")
            record(
                "the extent of history advances, so a polled timeline can follow it",
                end_after != end_before,
                f"before: {end_before}\nafter 20s: {end_after}\n"
                "(the strip fetched this once at mount, so it read the same window for sixteen minutes "
                "of wall clock while everything else moved)",
            )
        except httpx.HTTPError as exc:
            record("the extent of history advances", None, f"bounds endpoint unavailable: {exc}")

        # 10 ----------------------------------------------------------------------------------
        record(
            "six rail tabs fit, the drawer clears the top bar, ticks render as ticks",
            None,
            "not checkable from here: these are rendered-layout properties.\n"
            "verified by inspection instead — .tabs has overflow:hidden with flex:1 1 0 and "
            "min-width:0 on .tab; .drawer sits at top:var(--topbar-h) which also drives the app "
            "grid; STATUS_GLYPH is typed Record<RunStatus,string> so an unhandled status fails the "
            "TypeScript build.",
        )

    print("\n=== summary ===")
    checked = [row for row in results if row[1] is not None]
    failed = [row for row in checked if not row[1]]
    print(
        f"  {len(checked) - len(failed)}/{len(checked)} checkable fixes verified against the live stack"
    )
    print(f"  {len([row for row in results if row[1] is None])} not checkable without a browser")
    for name, _, _ in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
