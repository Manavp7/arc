"""The activities a playbook step runs — the actual side effects (PRD M15).

Each activity is idempotent on an `idempotency_key` derived from the run and step. That is not a nicety:
a step that times out may already have acted, so a retry without idempotency double-dispatches the drone.
The retry is the *expected* path here, not the exceptional one, because a timeout is how a slow network
looks.

Activities take a context object rather than reaching for globals, so the same code runs under the inline
runner and under Temporal — where activity code executes in a worker process that shares nothing with the
service that scheduled it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from sio_core import get_logger
from sio_schemas import utc_now

log = get_logger("sio.workflow.activities")


@dataclass
class ActivityContext:
    """Everything an activity needs, passed explicitly.

    Explicit rather than ambient because Temporal runs activities in a worker that shares no state with the
    scheduler. Code that reached for a module-level client would work inline and fail in the worker, which
    is the worst kind of difference: it only appears in the deployment you care about.
    """

    api_url: str
    ingest_url: str
    tenant_id: str
    run_id: str
    trigger_event_id: str | None = None
    zone_id: str | None = None
    entity_ids: list[str] = field(default_factory=list)
    dry_run: bool = False
    """When true, activities describe what they would do without doing it.

    The demo default for anything that would touch a real gate or a real drone. A workflow engine that can
    only be tested by actually closing a gate is a workflow engine nobody tests.
    """
    bearer_token: str = ""
    client: httpx.AsyncClient | None = None
    #: Effects recorded per idempotency key, so a retry returns the first result rather than acting again.
    ledger: dict[str, dict[str, Any]] = field(default_factory=dict)

    def key(self, step_id: str) -> str:
        return f"{self.run_id}:{step_id}"

    async def http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=10.0)
        return self.client

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None


def idempotent(function: Any) -> Any:
    """Return the first result for an idempotency key instead of acting twice.

    Wrapping rather than asking each activity to remember: the whole point is that this cannot be forgotten
    in one place. The ledger lives on the context, so it survives retries within a run and is deliberately
    *not* shared between runs — two fires should dispatch two drones.
    """

    async def wrapper(context: ActivityContext, step_id: str, **arguments: Any) -> dict[str, Any]:
        key = context.key(step_id)
        if key in context.ledger:
            recorded = dict(context.ledger[key])
            recorded["idempotent_replay"] = True
            log.info("workflow.activity_replayed", activity=function.__name__, key=key)
            return recorded
        result = await function(context, step_id, **arguments)
        if not result.get("error"):
            context.ledger[key] = result
        return result

    wrapper.__name__ = function.__name__
    wrapper.__doc__ = function.__doc__
    return wrapper


@idempotent
async def dispatch_drone(
    context: ActivityContext, step_id: str, **arguments: Any
) -> dict[str, Any]:
    """Send the patrol drone to the zone for visual confirmation.

    Confirmation first, because the fire rule trips on a heuristic detector at 0.35 confidence — closing a
    gate on the strength of that without a look would be acting on a guess.
    """
    zone = arguments.get("zone_id") or context.zone_id or "unknown"
    if context.dry_run:
        return {
            "dispatched": False,
            "dry_run": True,
            "zone_id": zone,
            "would": f"dispatch the patrol drone to {zone}",
        }
    # There is no drone command API in this build. Saying so is the honest outcome: reporting a dispatch
    # that did not happen would make the run record a lie, and the run record is what an incident review
    # reads.
    return {
        "dispatched": False,
        "zone_id": zone,
        "effect": "unsupported",
        "error": "no drone command interface in this build",
        "note": "no drone command interface in this build; no dispatch occurred",
        "ts": utc_now().isoformat(),
    }


@idempotent
async def recall_drone(context: ActivityContext, step_id: str, **arguments: Any) -> dict[str, Any]:
    """Undo a dispatch: bring the drone back."""
    return {
        "recalled": False,
        "zone_id": arguments.get("zone_id") or context.zone_id,
        "dry_run": context.dry_run,
        "effect": "dry_run" if context.dry_run else "unsupported",
        "would": "recall the patrol drone",
        "error": None if context.dry_run else "no drone command interface in this build",
    }


@idempotent
async def notify_security(
    context: ActivityContext, step_id: str, **arguments: Any
) -> dict[str, Any]:
    """Tell a human. Recorded as an event so it appears in the console and the audit trail."""
    team = arguments.get("team", "security")
    message = arguments.get(
        "message",
        f"Automated response for run {context.run_id}: attention required in "
        f"{context.zone_id or 'the yard'}.",
    )
    return {
        "team": team,
        "notified": False,
        "effect": "dry_run" if context.dry_run else "recorded_intent",
        "note": "recorded in the workflow event feed; no external notification was sent",
        "message": message,
        "channel": "event_feed",
        "dry_run": context.dry_run,
        "ts": utc_now().isoformat(),
    }


@idempotent
async def close_gate(context: ActivityContext, step_id: str, **arguments: Any) -> dict[str, Any]:
    """Close a gate to contain the area."""
    gate = arguments.get("gate_id") or _nearest_gate(context.zone_id)
    # Not closed either way, and saying so once is better than a conditional whose branches agree. There
    # is no gate actuator in this build, so reporting a closure would put a falsehood in the run record —
    # and the run record is what an incident review reads.
    return {
        "gate_id": gate,
        "closed": False,
        "dry_run": context.dry_run,
        "note": "no gate actuator in this build; the intent is recorded",
        "effect": "dry_run" if context.dry_run else "unsupported",
        "error": None if context.dry_run else "no gate actuator in this build",
        "would": f"close {gate}",
    }


@idempotent
async def open_gate(context: ActivityContext, step_id: str, **arguments: Any) -> dict[str, Any]:
    """Undo a closure. Runs during compensation, and matters more than the closure did.

    A rolled-back fire response that left a gate shut is worse than one that never ran: the site is
    blocked and no incident record says why.
    """
    gate = arguments.get("gate_id") or _nearest_gate(context.zone_id)
    return {
        "gate_id": gate,
        "reopened": False,
        "dry_run": context.dry_run,
        "effect": "dry_run" if context.dry_run else "unsupported",
        "would": f"reopen {gate}",
        "error": None if context.dry_run else "no gate actuator in this build",
    }


@idempotent
async def create_incident(
    context: ActivityContext, step_id: str, **arguments: Any
) -> dict[str, Any]:
    """Open an incident record so the response is auditable after the fact."""
    kind = arguments.get("kind", "fire")
    incident_id = f"inc_{context.run_id[-8:]}_{step_id}"
    return {
        "incident_id": incident_id,
        "effect": "dry_run" if context.dry_run else "workflow_record",
        "note": "incident details are stored in this workflow step; no external incident was opened",
        "kind": kind,
        "zone_id": context.zone_id,
        "trigger_event": context.trigger_event_id,
        "entities": context.entity_ids[:5],
        "opened_ts": utc_now().isoformat(),
        "dry_run": context.dry_run,
    }


@idempotent
async def close_incident(
    context: ActivityContext, step_id: str, **arguments: Any
) -> dict[str, Any]:
    """Undo an incident record — marked resolved rather than deleted.

    Deleting it would erase the fact that a response started, and the append-only tables exist precisely so
    that history is not editable. A compensated incident is a closed incident, not a missing one.
    """
    return {
        "incident_id": arguments.get("incident_id", f"inc_{context.run_id[-8:]}"),
        "resolved": False,
        "effect": "dry_run" if context.dry_run else "recorded_intent",
        "reason": "the response was rolled back; no external incident was changed",
        "dry_run": context.dry_run,
    }


@idempotent
async def generate_report(
    context: ActivityContext, step_id: str, **arguments: Any
) -> dict[str, Any]:
    """Produce a report of what happened, from the platform's own record.

    Reads back through the API rather than from the run's memory, so the report reflects what was actually
    persisted. A report assembled from in-process state would look right even when nothing was written.
    """
    started = time.perf_counter()
    events: list[dict[str, Any]] = []
    try:
        if not context.bearer_token:
            raise ValueError("no API credential configured for this report")
        client = await context.http()
        response = await client.get(
            f"{context.api_url}/api/events",
            params={"limit": 20},
            headers={"Authorization": f"Bearer {context.bearer_token}"},
            timeout=6.0,
        )
        response.raise_for_status()
        events = response.json()
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise ValueError("events API returned an invalid event list")
        if any(event.get("tenant_id") != context.tenant_id for event in events):
            raise ValueError("events API returned data outside the workflow tenant")
    except (httpx.HTTPError, ValueError) as exc:
        # An optional step, so this is recorded and the run continues. It is still reported: a report that
        # silently contained nothing would be worse than a missing one.
        return {
            "report": None,
            "error": f"could not read the event record: {exc}",
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }

    relevant = [
        event
        for event in events
        if (context.zone_id is not None and event.get("zone_id") == context.zone_id)
        or (
            context.trigger_event_id is not None
            and event.get("event_id") == context.trigger_event_id
        )
    ][:10]
    return {
        "report": {
            "run_id": context.run_id,
            "zone_id": context.zone_id,
            "trigger_event": context.trigger_event_id,
            "events_considered": len(events),
            "events_included": len(relevant),
            "timeline": [
                {
                    "ts": event.get("ts"),
                    "type": event.get("type"),
                    "severity": event.get("severity"),
                    "summary": (event.get("explanation") or {}).get("summary"),
                }
                for event in relevant
            ],
        },
        "ms": round((time.perf_counter() - started) * 1000, 1),
    }


def _nearest_gate(zone_id: str | None) -> str:
    """Which gate serves a zone.

    A lookup table rather than geometry: the spatial service owns geometry, and a workflow reaching for a
    second opinion on which gate is nearest would be a second implementation of a question already
    answered elsewhere. When the zone is unknown the main gate is the safe default, and it is named so the
    record shows a guess was made.
    """
    if not zone_id:
        return "gate_a"
    if "dock" in zone_id or "apron" in zone_id:
        return "gate_b"
    return "gate_a"


ACTIVITIES: dict[str, Any] = {
    "dispatch_drone": dispatch_drone,
    "recall_drone": recall_drone,
    "notify_security": notify_security,
    "close_gate": close_gate,
    "open_gate": open_gate,
    "create_incident": create_incident,
    "close_incident": close_incident,
    "generate_report": generate_report,
}

__all__ = ["ACTIVITIES", "ActivityContext", "idempotent"]
