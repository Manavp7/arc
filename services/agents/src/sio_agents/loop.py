"""The agent loop: observe, reason, decide, act, learn (PRD M14).

**An agent may not act on its own proposal.** That is the whole design, and it is worth being precise about
how it is enforced, because "we check a flag" is not enforcement.

The `act` step takes an *approved decision* as its input and has no other way in. It cannot construct one,
it cannot approve one, and the approval field it reads is written by a different service in response to an
HTTP call from a human. So the sequence is not "propose, then check whether we may act" — it is two separate
entries into the loop, one of which only a human can trigger. A bug in the reasoning cannot produce an
action, because reasoning does not have the argument that acting requires.

The rest of the loop is deliberately dull, which is also the point: an agent whose reasoning is
inscrutable cannot be trusted with an approval gate, because a human asked to approve something must be
able to see why it was proposed.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sio_core import get_logger
from sio_schemas import ApprovalState, Decision, utc_now

from .memory import AgentMemory, MemoryEntry, Recollection

log = get_logger("sio.agents.loop")


@dataclass
class Observation:
    """What an agent found when it looked."""

    agent: str
    situation: str
    """A sentence describing the state, used for memory retrieval and shown to the human."""
    facts: dict[str, Any] = field(default_factory=dict)
    zone_id: str | None = None
    interesting: bool = False
    """Whether this warrants reasoning at all.

    Most cycles are uninteresting, and an agent that proposes something every time it looks is an agent
    whose proposals get ignored. Deciding *not* to act is the common case and is recorded as such.
    """
    why_not: str | None = None


@dataclass
class Proposal:
    """What an agent wants a human to consider."""

    agent: str
    summary: str
    rationale: str
    zone_id: str | None = None
    kind: str = "security"
    urgency: str = "medium"
    memory: Recollection | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    def with_memory_caution(self) -> str:
        """The rationale, with any precedent from memory appended.

        An agent that has had this kind of proposal rejected must say so when proposing it again — the
        operator deserves the context, and repeating a rejected proposal without acknowledging it is how
        automation earns distrust.
        """
        caution = self.memory.caution if self.memory else None
        return f"{self.rationale} {caution}" if caution else self.rationale


@dataclass
class CycleResult:
    """One turn of the loop, recorded for the audit trail."""

    agent: str
    observed: Observation | None = None
    proposal: Proposal | None = None
    decision_id: str | None = None
    memory_entry: MemoryEntry | None = None
    skipped: str | None = None
    ms: float = 0.0

    @property
    def proposed(self) -> bool:
        return self.decision_id is not None

    def describe(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "situation": self.observed.situation if self.observed else None,
            "interesting": bool(self.observed and self.observed.interesting),
            "proposed": self.proposed,
            "decision_id": self.decision_id,
            "skipped": self.skipped,
            "memory": self.proposal.memory.describe()
            if self.proposal and self.proposal.memory
            else None,
            "ms": round(self.ms, 1),
        }


class Agent(Protocol):
    """What an agent must provide. Two of the five steps; the loop owns the rest."""

    name: str
    kind: str
    interval_s: float

    async def observe(self) -> Observation: ...

    async def reason(self, observation: Observation, memory: Recollection) -> Proposal | None: ...


#: Callback that files a proposal as a pending decision and returns its id.
Propose = Callable[[Proposal], Awaitable[str | None]]
#: Callback that carries out an APPROVED decision. Takes the decision, not an intention.
Execute = Callable[[Decision], Awaitable[dict[str, Any]]]
#: Callback that appends to the audit trail.
Audit = Callable[..., Awaitable[None]]


class AgentRunner:
    """Drives agents through the loop, and executes only what a human approved."""

    def __init__(
        self,
        agents: list[Agent],
        memory: AgentMemory,
        *,
        propose: Propose,
        execute: Execute,
        audit: Audit,
    ) -> None:
        self.agents = agents
        self.memory = memory
        self.propose = propose
        self.execute = execute
        self.audit = audit
        self._entries: dict[str, MemoryEntry] = {}
        """decision id → the memory entry awaiting its verdict."""
        self.cycles = 0
        self.proposals = 0
        self.executions = 0
        self.refusals = 0
        self._last_run: dict[str, float] = {}

    def due(self, agent: Agent, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        last = self._last_run.get(agent.name)
        return last is None or (now - last) >= agent.interval_s

    async def cycle(self, agent: Agent) -> CycleResult:
        """One turn: observe, recall, reason, propose. **Never act.**

        Acting is a separate entry point that requires an approved decision, so no path through this method
        can produce an action however the reasoning behaves.
        """
        started = time.perf_counter()
        self.cycles += 1
        self._last_run[agent.name] = time.monotonic()
        result = CycleResult(agent=agent.name)

        observation = await agent.observe()
        result.observed = observation
        if not observation.interesting:
            result.skipped = observation.why_not or "nothing worth proposing"
            result.ms = (time.perf_counter() - started) * 1000
            # Recorded, not silent. "I looked and there was nothing" is a useful audit entry, and its
            # absence makes a quiet agent indistinguishable from a stopped one.
            await self.audit(
                actor=f"agent:{agent.name}",
                action="observe",
                allowed=True,
                reason=result.skipped,
                details={"situation": observation.situation, "facts": observation.facts},
            )
            return result

        recollection = await self.memory.recall(observation.situation, agent=agent.name)
        proposal = await agent.reason(observation, recollection)
        if proposal is None:
            result.skipped = "the agent decided against proposing"
            result.ms = (time.perf_counter() - started) * 1000
            await self.audit(
                actor=f"agent:{agent.name}",
                action="reason",
                allowed=True,
                reason=result.skipped,
                details={"situation": observation.situation, "memory": recollection.describe()},
            )
            return result

        proposal.memory = recollection
        result.proposal = proposal
        decision_id = await self.propose(proposal)
        result.decision_id = decision_id
        if decision_id:
            self.proposals += 1
            entry = await self.memory.remember(
                agent=agent.name,
                situation=observation.situation,
                proposal=proposal.summary,
                decision_id=decision_id,
                zone_id=proposal.zone_id,
            )
            if entry is not None:
                self._entries[decision_id] = entry
                result.memory_entry = entry
            await self.audit(
                actor=f"agent:{agent.name}",
                action="propose",
                resource=decision_id,
                allowed=True,
                reason=proposal.summary,
                details={
                    "situation": observation.situation,
                    "rationale": proposal.with_memory_caution(),
                    "memory": recollection.describe(),
                    "awaiting_approval": True,
                },
            )
        result.ms = (time.perf_counter() - started) * 1000
        return result

    async def on_decision(self, decision: Decision) -> dict[str, Any] | None:
        """React to a decision's approval state. **The only path to action.**

        Takes a `Decision`, which the agent cannot manufacture: it comes off the bus, written by the
        decision service in response to an HTTP call from a human. An agent's reasoning has no way to reach
        this method with an approved decision in hand.
        """
        entry = self._entries.get(decision.decision_id)

        if decision.approval == ApprovalState.REJECTED:
            reason = _rejection_reason(decision)
            if entry is not None:
                await self.memory.record_outcome(entry, "rejected", reason=reason)
                self._entries.pop(decision.decision_id, None)
            await self.audit(
                actor="human",
                action="reject",
                resource=decision.decision_id,
                allowed=True,
                reason=reason,
                details={"proposed_by": decision.proposed_by},
            )
            log.info("agents.rejected", decision=decision.decision_id, reason=reason)
            return None

        if decision.approval != ApprovalState.APPROVED:
            # Pending: nothing to do. Including the agent's own proposal coming back around, which must not
            # be mistaken for a green light.
            return None

        if decision.executed_ts is not None:
            # Already carried out. Approval events can arrive twice under at-least-once delivery, and
            # executing twice is the failure this guard exists for.
            self.refusals += 1
            log.info("agents.already_executed", decision=decision.decision_id)
            return None

        chosen = next(
            (option for option in decision.options if option.option_id == decision.chosen), None
        )
        if chosen is None:
            # Approved, but pointing at no option. Refusing is right: guessing which option a human meant
            # is precisely the judgement the gate exists to keep with them.
            self.refusals += 1
            await self.audit(
                actor="agents",
                action="execute",
                resource=decision.decision_id,
                allowed=False,
                reason="approved but no option was chosen; refusing to guess",
                details={"options": [option.option_id for option in decision.options]},
            )
            log.error("agents.approved_without_option", decision=decision.decision_id)
            return None

        outcome = await self.execute(decision)
        self.executions += 1
        if entry is not None:
            await self.memory.record_outcome(
                entry,
                "executed" if outcome.get("ok", True) else "failed",
                reason=str(outcome.get("note") or "")[:200] or None,
            )
            self._entries.pop(decision.decision_id, None)
        await self.audit(
            actor="agents",
            action="execute",
            resource=decision.decision_id,
            allowed=True,
            reason=f"approved by {decision.approved_by or 'unknown'}",
            details={
                "option": chosen.option_id,
                "action": str(chosen.action),
                "approved_by": decision.approved_by,
                "approved_ts": decision.approved_ts.isoformat() if decision.approved_ts else None,
                "outcome": outcome,
            },
        )
        log.info(
            "agents.executed",
            decision=decision.decision_id,
            option=chosen.option_id,
            approved_by=decision.approved_by,
        )
        return outcome

    def describe(self) -> dict[str, Any]:
        return {
            "agents": [
                {
                    "name": agent.name,
                    "kind": agent.kind,
                    "interval_s": agent.interval_s,
                    "last_run_s_ago": (
                        round(time.monotonic() - self._last_run[agent.name], 1)
                        if agent.name in self._last_run
                        else None
                    ),
                }
                for agent in self.agents
            ],
            "cycles": self.cycles,
            "proposals": self.proposals,
            "executions": self.executions,
            "refused_executions": self.refusals,
            "awaiting_verdict": len(self._entries),
            "memory": self.memory.describe(),
        }


def _rejection_reason(decision: Decision) -> str | None:
    """Dig the human's reason out of the explanation, where the decision service put it."""
    for note in reversed(decision.explanation.notes):
        if note.startswith("rejected by"):
            _, _, tail = note.partition(":")
            return tail.strip() or note
    return None


__all__ = [
    "Agent",
    "AgentRunner",
    "Audit",
    "CycleResult",
    "Execute",
    "Observation",
    "Proposal",
    "Propose",
    "utc_now",
]
