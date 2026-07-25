"""The security and logistics agents (PRD M14).

Both are deliberately simple, and their reasoning is deliberately legible. An agent whose reasoning is
inscrutable cannot be trusted with an approval gate: a human asked to approve something must be able to see
why it was proposed, and "the model said so" is not a reason anyone can act on.

So each agent's `reason` is a short chain of stated conditions over observed facts, and every proposal
carries the numbers it was based on. Where a model could help — phrasing, summarising — it is used for
*wording*, never for the decision, for the same reason the decision service keeps the optimiser in charge
of the ranking.

Both agents look for things nothing else is watching for. There is no point in an agent that re-raises what
the rule engine already fires: the events engine notices a fire, and an agent that also notices the fire has
added a second voice saying the same thing. What an agent can add is *patterns over time* — a zone that has
been quiet for too long, a queue that keeps growing — which a stateless rule cannot see.
"""

from __future__ import annotations

from typing import Any

import httpx

from sio_core import describe_error, get_logger

from .loop import Observation, Proposal, Recollection

log = get_logger("sio.agents.agents")

TIMEOUT_S = 6.0


class SecurityAgent:
    """Watches for coverage the site does not have, and for restricted zones going unobserved.

    Not a second fire detector. The events engine already fires on detections; this agent looks for
    something a stateless rule cannot see — **a restricted zone with no recent observation at all.** An
    empty event stream from a fuel store is either a quiet fuel store or a blind one, and the difference
    matters enough to ask a human about.
    """

    name = "security"
    kind = "security"
    interval_s = 180.0

    #: Zones where "nothing reported" is worth asking about rather than reassuring.
    SENSITIVE = ("fuel_store", "warehouse", "office")
    #: How long a sensitive zone may go unobserved before it is worth raising.
    QUIET_LIMIT_S = 900.0

    def __init__(self, api_url: str, spatial_url: str, client: httpx.AsyncClient) -> None:
        self.api_url = api_url.rstrip("/")
        self.spatial_url = spatial_url.rstrip("/")
        self.client = client

    async def observe(self) -> Observation:
        coverage: dict[str, Any] = {}
        quiet: list[str] = []
        try:
            response = await self.client.get(
                f"{self.spatial_url}/spatial/blind_spots", timeout=TIMEOUT_S
            )
            if response.status_code == 200:
                coverage = response.json()
            zones = await self.client.get(f"{self.spatial_url}/spatial/zones", timeout=TIMEOUT_S)
            occupancy = {
                zone["zone_id"]: zone.get("occupancy", 0)
                for zone in (zones.json().get("zones", []) if zones.status_code == 200 else [])
            }
            events = await self.client.get(
                f"{self.api_url}/api/events", params={"limit": 100}, timeout=TIMEOUT_S
            )
            seen_zones = {
                event.get("zone_id")
                for event in (events.json() if events.status_code == 200 else [])
                if event.get("zone_id")
            }
            quiet = [
                zone for zone in self.SENSITIVE if zone in occupancy and zone not in seen_zones
            ]
        except httpx.HTTPError as exc:
            # An agent that cannot see must say so rather than concluding all is well. Silence from a broken
            # sensor and silence from a quiet site look identical from here, and only one is fine.
            return Observation(
                agent=self.name,
                situation="the security agent could not read the platform",
                interesting=False,
                why_not=f"observation failed: {exc}",
            )

        fraction = float(coverage.get("coverage_fraction") or 0.0)
        uncovered = float(coverage.get("uncovered_m2") or 0.0)
        situation = (
            f"security check: camera coverage {fraction:.0%} of the site, {uncovered:,.0f} m2 "
            f"uncovered; sensitive zones with no recent events: {', '.join(quiet) or 'none'}"
        )
        interesting = bool(quiet) or (fraction < 0.2 and uncovered > 50_000)
        return Observation(
            agent=self.name,
            situation=situation,
            facts={
                "coverage_fraction": round(fraction, 3),
                "uncovered_m2": round(uncovered, 1),
                "quiet_sensitive_zones": quiet,
            },
            zone_id=quiet[0] if quiet else None,
            interesting=interesting,
            why_not=None
            if interesting
            else "coverage is adequate and no sensitive zone is unobserved",
        )

    async def reason(self, observation: Observation, memory: Recollection) -> Proposal | None:
        quiet = observation.facts.get("quiet_sensitive_zones") or []
        fraction = observation.facts.get("coverage_fraction", 1.0)

        if quiet:
            zone = quiet[0]
            return Proposal(
                agent=self.name,
                kind="intrusion",
                zone_id=zone,
                urgency="medium",
                summary=f"Send the patrol drone over {zone} for a look",
                rationale=(
                    f"{zone} is occupied but has produced no events recently, which means either nothing "
                    f"is happening there or nothing is watching it. A single overflight distinguishes the "
                    f"two. Camera coverage across the site is {fraction:.0%}."
                ),
                facts=observation.facts,
            )

        # Low coverage is a real finding but not an incident: proposing a patrol for it every three minutes
        # would be noise. Raised once, with the number, and left to memory to suppress thereafter.
        if memory.found and not memory.rejections:
            return None
        return Proposal(
            agent=self.name,
            kind="intrusion",
            zone_id=None,
            urgency="low",
            summary="Review camera placement: most of the site is not covered",
            rationale=(
                f"Only {fraction:.0%} of the site is within a camera's field of view, leaving "
                f"{observation.facts.get('uncovered_m2', 0):,.0f} m2 unobserved. This is a placement "
                f"question rather than an incident, so it is raised once."
            ),
            facts=observation.facts,
        )


class LogisticsAgent:
    """Watches dock turnaround and queue growth.

    Again, not a duplicate of a rule: `dwell_exceeded` fires when *one* truck has been too long. This agent
    looks at the *queue* — several trucks waiting while docks sit free is a scheduling problem no per-entity
    rule can see, because no single truck is misbehaving.
    """

    name = "logistics"
    kind = "logistics"
    interval_s = 240.0

    #: Trucks on site with no dock, above which a schedule is worth proposing.
    QUEUE_LIMIT = 3

    def __init__(
        self, api_url: str, spatial_url: str, decision_url: str, client: httpx.AsyncClient
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.spatial_url = spatial_url.rstrip("/")
        self.decision_url = decision_url.rstrip("/")
        self.client = client

    async def observe(self) -> Observation:
        try:
            trucks_response = await self.client.get(
                f"{self.api_url}/api/entities",
                params={
                    "type": "truck",
                    "include_static": False,
                    "active_within_s": 600,
                    "limit": 50,
                },
                timeout=TIMEOUT_S,
            )
            zones_response = await self.client.get(
                f"{self.spatial_url}/spatial/zones", timeout=TIMEOUT_S
            )
        except httpx.HTTPError as exc:
            return Observation(
                agent=self.name,
                situation="the logistics agent could not read the platform",
                interesting=False,
                why_not=f"observation failed: {exc}",
            )

        trucks = trucks_response.json() if trucks_response.status_code == 200 else []
        zones = zones_response.json().get("zones", []) if zones_response.status_code == 200 else []
        docks = [zone for zone in zones if zone.get("kind") == "dock"]
        occupied = [dock for dock in docks if dock.get("occupancy", 0) > 0]
        free = [dock["zone_id"] for dock in docks if dock.get("occupancy", 0) == 0]

        in_a_dock = {
            entity.get("entity_id")
            for entity in trucks
            if (entity.get("state") or {}).get("zone_id", "").startswith("dock")
        }
        waiting = [entity for entity in trucks if entity.get("entity_id") not in in_a_dock]

        situation = (
            f"logistics check: {len(waiting)} truck(s) waiting, {len(occupied)}/{len(docks)} docks in use, "
            f"{len(free)} free"
        )
        # Both conditions must hold. Trucks waiting while every dock is busy is a capacity problem an agent
        # cannot fix, and proposing a schedule for it would be theatre.
        interesting = len(waiting) >= self.QUEUE_LIMIT and bool(free)
        return Observation(
            agent=self.name,
            situation=situation,
            facts={
                "waiting": len(waiting),
                "docks_total": len(docks),
                "docks_free": len(free),
                "free_docks": free[:6],
                "waiting_labels": [entity.get("label") for entity in waiting[:6]],
            },
            interesting=interesting,
            why_not=(
                None
                if interesting
                else (
                    f"{len(waiting)} waiting with {len(free)} dock(s) free — "
                    + ("no free dock to assign" if not free else "the queue is short")
                )
            ),
        )

    async def reason(self, observation: Observation, memory: Recollection) -> Proposal | None:
        waiting = observation.facts.get("waiting", 0)
        free = observation.facts.get("free_docks") or []
        labels = [label for label in observation.facts.get("waiting_labels", []) if label]

        # A concrete schedule, so the human is approving a plan rather than a sentiment. Computed by the
        # decision service, which owns the solver — an agent forming its own opinion about dock allocation
        # would be a second scheduler to keep in agreement with the first.
        schedule: dict[str, Any] | None = None
        try:
            response = await self.client.post(
                f"{self.decision_url}/decisions/schedule/docks", timeout=TIMEOUT_S
            )
            if response.status_code == 200:
                schedule = response.json()
        except httpx.HTTPError as exc:
            log.info("agents.schedule_unavailable", error=describe_error(exc))

        detail = ""
        if schedule and schedule.get("slots"):
            worst = schedule.get("worst_wait_s", 0)
            detail = (
                f" A schedule exists that clears them with a worst wait of {worst / 60:.0f} min "
                f"across {len(schedule['slots'])} assignment(s)."
            )

        return Proposal(
            agent=self.name,
            kind="congestion",
            zone_id=free[0] if free else None,
            urgency="medium",
            summary=f"Assign the {waiting} waiting truck(s) to the {len(free)} free dock(s)",
            rationale=(
                f"{waiting} truck(s) are on site without a dock while {len(free)} dock(s) are free"
                + (f" ({', '.join(free[:3])})" if free else "")
                + f".{detail}"
                + (f" Waiting: {', '.join(labels[:3])}." if labels else "")
            ),
            facts={**observation.facts, "schedule": schedule},
        )


__all__ = ["LogisticsAgent", "SecurityAgent"]
