"""The SDK quickstart, as a script that runs (PRD M22, Phase 6).

`docs/SDK.md` embeds this file rather than paraphrasing it, and a test asserts the two cannot drift. That is
deliberate: the plan's acceptance for the SDK is "the quickstart in docs/SDK.md runs", and a quickstart copied
into prose is a quickstart that stops running the first time an API changes — while still looking correct.

    uv run python examples/sdk_quickstart.py

Needs a running platform. Everything here is read-only except the what-if projection, which changes nothing by
construction.
"""

from __future__ import annotations

import asyncio

from sio_sdk import SioApiError, SioClient


async def main() -> None:
    # No token needed in dev mode: the client obtains one, reuses it, and renews on a 401. In a Keycloak
    # deployment, pass `token=...` from wherever your identity provider put it.
    async with SioClient(subject="quickstart", roles=("operator", "commander"), clearance=2) as sio:
        session = await sio.authenticate()
        print(
            f"connected as {session.subject!r} in tenant {session.tenant!r}, roles {list(session.roles)}"
        )

        # --- what is on site --------------------------------------------------------------------
        entities = await sio.entities(limit=200)
        by_type: dict[str, int] = {}
        for entity in entities:
            by_type[str(entity.type)] = by_type.get(str(entity.type), 0) + 1
        print(f"\n{len(entities)} moving entities: {by_type}")

        if entities:
            # Typed, so this is attribute access rather than three chained dict lookups that fail at the point
            # of use when a field is renamed.
            first = entities[0]
            zone = first.state.zone_id or "no zone"
            print(f"  e.g. {first.label or first.entity_id} ({first.type}) in {zone}")

        # --- what has happened ------------------------------------------------------------------
        events = await sio.events(limit=10)
        print(f"\n{len(events)} recent events:")
        for event in events[:3]:
            print(f"  {event.severity:8} {event.type:22} {event.explanation.summary[:56]}")

        alerts = await sio.alerts(limit=5)
        print(f"\n{len(alerts)} alerts needing attention:")
        for alert in alerts[:3]:
            print(f"  priority {alert.score:6.1f}  {alert.title[:58]}")
            # The score never travels without the sentence that justifies it.
            print(f"                  {alert.urgency_reason}")

        # --- how the site is doing --------------------------------------------------------------
        analytics = await sio.analytics(hours=24)
        risk = analytics["risk"]
        print(f"\nrisk: {risk['score']} / 100 ({risk['band']})")
        for driver in risk["drivers"][:3]:
            print(f"  - {driver}")
        dwell = analytics["dwell"]["overall"]
        if dwell["count"]:
            # The shape sentence is the part a chart cannot tell you.
            print(f"  dwell: {dwell['shape']}")

        # --- what would happen if ---------------------------------------------------------------
        zones = await sio.zones()
        if zones:
            target = next(
                (zone["zone_id"] for zone in zones if "fuel" in zone["zone_id"]),
                zones[0]["zone_id"],
            )
            projection = await sio.simulate(
                "fire_spread", zone_id=target, duration_s=1800, wind_speed_mps=5
            )
            print(f"\nwhat-if: {projection['results']['summary'][:100]}")
            print(f"  impact: {projection['kpi_deltas']}")
            print("  (a projection against a frozen copy of the world; nothing on site changed)")

        # --- ask in English ----------------------------------------------------------------------
        # Slow by nature: a local 3B model takes seconds, not milliseconds.
        answer = await sio.ask("What is on site right now?")
        print(f"\nasked: {answer.question}")
        print(f"  {answer.text[:150]}")
        print(f"  via {answer.tools_used} at {answer.confidence:.0%} confidence")
        if answer.was_redacted:
            print(f"  {answer.redaction}")

        # --- the permission gate is real ---------------------------------------------------------
        # Approving a recommendation needs a commander AND clearance 2. This client has both, so the failure
        # below is a 404 (no such decision) rather than a 403 — which is the point: the gate is policy, not a
        # UI affordance, and an SDK cannot route around it.
        try:
            await sio.approve("dec_does_not_exist")
        except SioApiError as error:
            verdict = "refused by policy" if error.is_permission_error else "reached the service"
            print(f"\napproval attempt {verdict}: {error.detail[:90]}")

        # --- live updates ------------------------------------------------------------------------
        print("\nstreaming three live messages:")
        seen = 0
        async for message in sio.subscribe("events", "alerts"):
            print(
                f"  {message.kind}: {str(message.payload.get('type') or message.payload.get('title'))[:56]}"
            )
            seen += 1
            if seen >= 3:
                break

        print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
