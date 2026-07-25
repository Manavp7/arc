"""Agents service: scheduled loops that propose, and execute only what a human approved (PRD M14)."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

from sio_core import (
    MessageContext,
    PgPool,
    SioService,
    get_embedder,
    get_pg_pool,
    get_vectors,
)
from sio_schemas import BusMessage, Decision, Topic, new_id, utc_now

from .agents import LogisticsAgent, SecurityAgent
from .loop import AgentRunner, Proposal
from .memory import AgentMemory


class AgentsService(SioService):
    """Two agents, a memory, and an approval gate it cannot open itself."""

    name = "agents"
    subscribes = (Topic.DECISIONS,)
    tick_interval_s = 30.0
    """The tick only asks "is any agent due?". Each agent has its own interval, because a security sweep and
    a dock review do not want the same cadence, and a single shared period would be wrong for both."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self.client = httpx.AsyncClient(timeout=10.0)
        self.memory = AgentMemory(
            get_vectors(self.settings),
            get_embedder(self.settings),
            tenant_id=self.settings.tenant_id,
        )
        api = f"http://127.0.0.1:{self.settings.api_port}"
        spatial = f"http://127.0.0.1:{self.settings.spatial_port}"
        decision = f"http://127.0.0.1:{self.settings.decision_port}"
        self.workflow_url = f"http://127.0.0.1:{self.settings.workflow_port}"
        self.decision_url = decision
        self.runner = AgentRunner(
            [
                SecurityAgent(api, spatial, self.client),
                LogisticsAgent(api, spatial, decision, self.client),
            ],
            self.memory,
            propose=self._propose,
            execute=self._execute,
            audit=self._audit,
        )
        self._cycles: list[dict[str, Any]] = []

    async def setup(self) -> None:
        await self.pool.open()
        self.log.info(
            "agents.ready",
            agents=[agent.name for agent in self.runner.agents],
            memory=self.memory.describe(),
            gate="proposals require POST /decisions/{id}/approve; this service cannot approve",
        )
        if self.memory.unavailable:
            self.log.warning(
                "agents.memory_unavailable",
                effect="agents will not recall precedents, and will not pretend to",
            )

    async def teardown(self) -> None:
        await self.client.aclose()

    async def health_checks(self) -> dict[str, str]:
        return {
            "postgres": "ok" if await self.pool.ping() else "unreachable",
            "memory": "ok"
            if not self.memory.unavailable
            else "degraded: no vector store or embedder",
        }

    async def health_info(self) -> dict[str, str]:
        state = self.runner.describe()
        return {
            "cycles": str(state["cycles"]),
            "proposals": str(state["proposals"]),
            "executions": str(state["executions"]),
            "refused_executions": str(state["refused_executions"]),
            "awaiting_verdict": str(state["awaiting_verdict"]),
        }

    # ---------------------------------------------------------------------- loop
    async def tick(self) -> None:
        for agent in self.runner.agents:
            if not self.runner.due(agent):
                continue
            result = await self.runner.cycle(agent)
            self._cycles.append(result.describe())
            del self._cycles[: max(0, len(self._cycles) - 40)]
            self.log.info(
                "agents.cycle",
                agent=agent.name,
                proposed=result.proposed,
                decision=result.decision_id,
                skipped=result.skipped,
                ms=round(result.ms),
            )

    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        """React to a decision's approval. The only path from proposal to action."""
        if message.kind != "Decision":
            return
        decision = message.decode(Decision)
        await self.runner.on_decision(decision)

    # ------------------------------------------------------------------ callbacks
    async def _propose(self, proposal: Proposal) -> str | None:
        """File a proposal as a PENDING decision, via the decision service.

        Deliberately over HTTP to the service that owns decisions, rather than writing the row directly. A
        service that can insert its own approved decision is one approval-field bug away from acting without
        a human, and the point of the gate is that no such bug is reachable from here.
        """
        try:
            response = await self.client.post(
                f"{self.decision_url}/decisions/recommend",
                params={
                    "kind": proposal.kind,
                    "task": proposal.task,
                    "zone_id": proposal.zone_id or "yard",
                    "severity": {"low": "high", "medium": "high", "high": "critical"}.get(
                        proposal.urgency, "high"
                    ),
                },
                # Generous, because the decision service solves three times AND may ask a model to write the
                # rationale. Twenty seconds was not enough and the proposal was silently lost — measured, and
                # the log line said nothing because a timeout's str() is empty.
                timeout=60.0,
            )
            if response.status_code != 200:
                self.log.warning(
                    "agents.propose_failed", status=response.status_code, body=response.text[:200]
                )
                return None
            decision = response.json()
        except httpx.HTTPError as exc:
            # The TYPE, not just the message. httpx timeouts stringify to an empty string, so `error=describe_error(exc)`
            # logged `error=` and told me nothing at all about why proposals were vanishing.
            self.log.warning(
                "agents.propose_unreachable",
                error=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
                url=f"{self.decision_url}/decisions/recommend",
            )
            return None

        decision_id = str(decision.get("decision_id", ""))
        # Attach the agent's own reasoning to the decision, so the human approving it sees WHY an agent
        # raised this rather than only the optimiser's ranking. Two different kinds of reason, both needed.
        await self.pool.execute(
            """
            UPDATE decisions
               SET payload = jsonb_set(
                     jsonb_set(payload, '{proposed_by}', %s::jsonb, true),
                     '{rationale}', %s::jsonb, true
                   )
             WHERE tenant_id = %s AND decision_id = %s
            """,
            (
                f'"agent:{proposal.agent}"',
                _json_string(f"{proposal.with_memory_caution()} (via the {proposal.agent} agent)"),
                self.settings.tenant_id,
                decision_id,
            ),
        )
        return decision_id or None

    async def _execute(self, decision: Decision) -> dict[str, Any]:
        """Carry out an approved decision by starting the matching playbook.

        The workflow service owns execution — retries, compensation, per-step progress — so an agent asked to
        act starts a playbook rather than reaching for an actuator. That keeps one implementation of "how to
        do a thing safely" instead of two.
        """
        chosen = next(
            (option for option in decision.options if option.option_id == decision.chosen), None
        )
        playbook = _playbook_for(str(chosen.action) if chosen else "")
        if playbook is None:
            return {
                "ok": True,
                "executed": False,
                "note": (
                    f"the approved option ({chosen.action if chosen else 'unknown'}) has no playbook; "
                    "nothing was run and this is recorded rather than assumed"
                ),
            }
        try:
            response = await self.client.post(
                f"{self.workflow_url}/workflow/run/{playbook}",
                params={"zone_id": _zone_of(decision) or "dock_3"},
                timeout=60.0,
            )
            response.raise_for_status()
            run = response.json()
        except httpx.HTTPError as exc:
            return {"ok": False, "executed": False, "note": f"the workflow service failed: {exc}"}
        return {
            "ok": run.get("status") == "completed",
            "executed": True,
            "playbook": playbook,
            "run_id": run.get("run_id"),
            "status": run.get("status"),
            "note": f"{playbook} finished as {run.get('status')}",
        }

    async def _audit(
        self,
        *,
        actor: str,
        action: str,
        resource: str | None = None,
        allowed: bool = True,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append to the audit log.

        Append-only and enforced by a trigger, which is the point: an agent's trail is worth having precisely
        because nothing — including the agent — can edit it afterwards.
        """
        import json

        await self.pool.execute(
            """
            INSERT INTO audit_log (
                tenant_id, audit_id, ts, actor, actor_roles, action, resource, allowed, reason, details
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                self.settings.tenant_id,
                new_id("aud"),
                utc_now(),
                actor,
                ["agent"] if actor.startswith("agent:") else ["operator"],
                action,
                resource,
                allowed,
                reason,
                json.dumps(details or {}, default=str),
            ),
        )

    # -------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/agents", tags=["agents"])
        async def describe() -> dict[str, Any]:
            """What the agents are, when they last ran, and what they have proposed."""
            return {
                **self.runner.describe(),
                "gate": (
                    "an agent may not act on its own proposal: execution requires an approved Decision, "
                    "which only a human can produce via POST /decisions/{id}/approve"
                ),
            }

        @app.get("/agents/cycles", tags=["agents"])
        async def cycles() -> dict[str, Any]:
            """Recent loop turns, including the ones that proposed nothing.

            The quiet cycles are the point: "I looked and there was nothing" is a useful record, and its
            absence makes a quiet agent indistinguishable from a stopped one.
            """
            return {"cycles": list(reversed(self._cycles))}

        @app.post("/agents/{agent_name}/run", tags=["agents"])
        async def run_now(agent_name: str) -> dict[str, Any]:
            """Run one agent's loop immediately. Still cannot act — only propose."""
            agent = next(
                (candidate for candidate in self.runner.agents if candidate.name == agent_name),
                None,
            )
            if agent is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"unknown agent {agent_name!r}; have "
                    f"{[candidate.name for candidate in self.runner.agents]}",
                )
            result = await self.runner.cycle(agent)
            self._cycles.append(result.describe())
            return result.describe()

        @app.get("/agents/audit", tags=["agents"])
        async def audit(limit: int = 40) -> dict[str, Any]:
            """The audit trail: every observation, proposal, verdict and execution."""
            rows = await self.pool.fetch(
                """
                SELECT ts, actor, action, resource, allowed, reason, details
                  FROM audit_log
                 WHERE tenant_id = %s AND (actor LIKE 'agent:%%' OR actor IN ('agents', 'human'))
                 ORDER BY ts DESC LIMIT %s
                """,
                (self.settings.tenant_id, min(limit, 200)),
            )
            return {
                "entries": [
                    {
                        "ts": row["ts"].isoformat(),
                        "actor": row["actor"],
                        "action": row["action"],
                        "resource": row["resource"],
                        "allowed": row["allowed"],
                        "reason": row["reason"],
                        "details": row["details"],
                    }
                    for row in rows
                ]
            }

        @app.get("/agents/memory", tags=["agents"])
        async def memory(situation: str = "trucks waiting for a dock") -> dict[str, Any]:
            """What memory would recall for a given situation — the "learn" step, made inspectable."""
            recollection = await self.memory.recall(situation)
            return {"situation": situation, **recollection.describe()}


def _playbook_for(action: str) -> str | None:
    """Which playbook carries out an action.

    Returns None rather than a default. A decision approved for an action with no playbook must be reported
    as un-executed, not quietly run as something else — substituting an action a human did not approve is
    the one thing this whole design exists to prevent.
    """
    return {
        "dispatch_drone": "IntrusionPlaybook",
        "dispatch_patrol": "IntrusionPlaybook",
        "close_gate": "FireResponsePlaybook",
        "increase_security": "IntrusionPlaybook",
    }.get(action)


def _zone_of(decision: Decision) -> str | None:
    for option in decision.options:
        plan = option.params.get("plan") or []
        for entry in plan:
            if entry.get("zone_id"):
                return str(entry["zone_id"])
    return None


def _json_string(value: str) -> str:
    import json

    return json.dumps(value)


__all__ = ["AgentsService"]
