"""Tests for agents (PRD M14).

The acceptance criterion is: an agent proposes, **nothing executes without approval**, approval triggers the
action, and it is audited. So the tests that matter most are the ones that try to make an agent act without a
human — including by making its reasoning misbehave, which is the realistic failure.

The design being tested is structural rather than conditional: `act` takes an *approved Decision* as its
argument and has no other entry, so a bug in reasoning cannot reach it. These tests try anyway.
"""

from __future__ import annotations

from typing import Any

from sio_agents import AgentMemory, AgentRunner, MemoryEntry, Observation, Proposal, Recollection
from sio_agents.memory import SIMILARITY_FLOOR

from sio_schemas import (
    ActionType,
    ApprovalState,
    Decision,
    DecisionOption,
    Explanation,
    new_id,
    utc_now,
)


class StubAgent:
    """An agent whose observation and reasoning are dictated by the test."""

    kind = "test"

    def __init__(
        self,
        *,
        name: str = "stub",
        interesting: bool = True,
        proposal: Proposal | None = None,
        interval_s: float = 0.0,
    ) -> None:
        self.name = name
        self.interval_s = interval_s
        self._interesting = interesting
        self._proposal = proposal
        self.observed = 0
        self.reasoned = 0
        self.seen_memory: Recollection | None = None

    async def observe(self) -> Observation:
        self.observed += 1
        return Observation(
            agent=self.name,
            situation="three trucks waiting with two docks free",
            facts={"waiting": 3},
            interesting=self._interesting,
            why_not=None if self._interesting else "nothing worth proposing",
        )

    async def reason(self, observation: Observation, memory: Recollection) -> Proposal | None:
        self.reasoned += 1
        self.seen_memory = memory
        if self._proposal is None:
            return None
        return self._proposal


class FakeMemory(AgentMemory):
    """Memory with no vector store, so tests exercise the loop rather than pgvector."""

    def __init__(self, precedents: list[MemoryEntry] | None = None) -> None:
        super().__init__(None, None, tenant_id="acme")
        self.precedents = precedents or []
        self.outcomes: list[tuple[str, str, str | None]] = []

    async def recall(
        self, situation: str, *, agent: str | None = None, limit: int = 3
    ) -> Recollection:
        return Recollection(precedents=list(self.precedents), searched=len(self.precedents))

    async def record_outcome(
        self, entry: MemoryEntry, outcome: str, *, reason: str | None = None
    ) -> None:
        self.outcomes.append((entry.memory_id, outcome, reason))
        await super().record_outcome(entry, outcome, reason=reason)


def a_proposal(**kwargs: Any) -> Proposal:
    defaults: dict[str, Any] = {
        "agent": "stub",
        "summary": "Send the drone over the fuel store",
        "rationale": "It is occupied and has produced no events for 20 minutes.",
        "zone_id": "fuel_store",
        "kind": "intrusion",
    }
    defaults.update(kwargs)
    return Proposal(**defaults)


def a_decision(
    *,
    approval: ApprovalState = ApprovalState.PENDING,
    chosen: bool = True,
    executed: bool = False,
    action: ActionType = ActionType.DISPATCH_DRONE,
    notes: list[str] | None = None,
) -> Decision:
    option = DecisionOption(
        action=action,
        score=10.0,
        expected_effect="Drone 0018 arrives in about 40s",
        params={"plan": [{"zone_id": "fuel_store"}]},
    )
    return Decision(
        tenant_id="acme",
        decision_id=new_id("dec"),
        options=[option],
        chosen=option.option_id if chosen else None,
        rationale="t",
        approval=approval,
        approved_by="operator-jane" if approval == ApprovalState.APPROVED else None,
        approved_ts=utc_now() if approval != ApprovalState.PENDING else None,
        executed_ts=utc_now() if executed else None,
        explanation=Explanation(notes=notes or []),
    )


def a_runner(
    agents: list[Any], memory: AgentMemory | None = None, *, execute_ok: bool = True
) -> tuple[AgentRunner, dict[str, list[Any]]]:
    log: dict[str, list[Any]] = {"proposed": [], "executed": [], "audit": []}

    async def propose(proposal: Proposal) -> str | None:
        log["proposed"].append(proposal)
        return new_id("dec")

    async def execute(decision: Decision) -> dict[str, Any]:
        log["executed"].append(decision)
        return {"ok": execute_ok, "executed": True, "note": "ran"}

    async def audit(**entry: Any) -> None:
        log["audit"].append(entry)

    return (
        AgentRunner(agents, memory or FakeMemory(), propose=propose, execute=execute, audit=audit),
        log,
    )


# ------------------------------------------------------------------ the gate
async def test_a_cycle_proposes_and_never_executes() -> None:
    """The core property. A full loop turn produces a proposal and no action, whatever the agent does."""
    agent = StubAgent(proposal=a_proposal())
    runner, log = a_runner([agent])

    result = await runner.cycle(agent)

    assert result.proposed
    assert len(log["proposed"]) == 1
    assert log["executed"] == [], "a cycle must never execute anything"
    assert runner.executions == 0


async def test_a_pending_decision_does_not_execute() -> None:
    """Including the agent's own proposal coming back around, which must not be mistaken for approval."""
    runner, log = a_runner([StubAgent()])
    outcome = await runner.on_decision(a_decision(approval=ApprovalState.PENDING))
    assert outcome is None
    assert log["executed"] == []


async def test_a_rejected_decision_does_not_execute() -> None:
    runner, log = a_runner([StubAgent()])
    await runner.on_decision(a_decision(approval=ApprovalState.REJECTED))
    assert log["executed"] == []


async def test_an_approved_decision_executes_and_is_audited() -> None:
    """Approval triggers the action, and the trail records who approved it."""
    runner, log = a_runner([StubAgent()])
    decision = a_decision(approval=ApprovalState.APPROVED)

    outcome = await runner.on_decision(decision)

    assert outcome is not None and outcome["executed"] is True
    assert len(log["executed"]) == 1
    entry = next(item for item in log["audit"] if item["action"] == "execute")
    assert entry["allowed"] is True
    assert "operator-jane" in entry["reason"]
    assert entry["details"]["approved_by"] == "operator-jane"
    assert entry["resource"] == decision.decision_id


async def test_an_already_executed_decision_is_not_executed_twice() -> None:
    """Approval events arrive twice under at-least-once delivery, and executing twice is the failure this
    guard exists for."""
    runner, log = a_runner([StubAgent()])
    await runner.on_decision(a_decision(approval=ApprovalState.APPROVED, executed=True))
    assert log["executed"] == []
    assert runner.refusals == 1


async def test_an_approval_with_no_chosen_option_is_refused_not_guessed() -> None:
    """Guessing which option a human meant is exactly the judgement the gate exists to keep with them."""
    runner, log = a_runner([StubAgent()])
    await runner.on_decision(a_decision(approval=ApprovalState.APPROVED, chosen=False))

    assert log["executed"] == []
    assert runner.refusals == 1
    refusal = next(item for item in log["audit"] if item["allowed"] is False)
    assert "refusing to guess" in refusal["reason"]


async def test_a_misbehaving_agent_still_cannot_act() -> None:
    """The realistic failure: reasoning goes wrong. It must not be able to produce an action.

    `cycle` has no execute callback in scope and `on_decision` requires a Decision the agent cannot
    manufacture, so this is structural rather than a check that could be forgotten.
    """

    class Overreaching(StubAgent):
        async def reason(self, observation: Observation, memory: Recollection) -> Proposal:
            # An agent trying to claim urgency, authority, anything.
            return a_proposal(summary="EXECUTE IMMEDIATELY", urgency="high")

    agent = Overreaching(proposal=a_proposal())
    runner, log = a_runner([agent])
    await runner.cycle(agent)
    assert log["executed"] == []


# ------------------------------------------------------------------- the loop
async def test_an_uninteresting_observation_is_recorded_not_silent() -> None:
    """ "I looked and there was nothing" is a useful audit entry, and its absence makes a quiet agent
    indistinguishable from a stopped one."""
    agent = StubAgent(interesting=False)
    runner, log = a_runner([agent])

    result = await runner.cycle(agent)

    assert not result.proposed
    assert result.skipped
    assert agent.reasoned == 0, "reasoning should not run when there is nothing to reason about"
    assert any(item["action"] == "observe" for item in log["audit"])


async def test_an_agent_may_decline_to_propose_after_reasoning() -> None:
    agent = StubAgent(proposal=None)
    runner, log = a_runner([agent])
    result = await runner.cycle(agent)
    assert not result.proposed
    assert agent.reasoned == 1
    assert log["proposed"] == []
    assert any(item["action"] == "reason" for item in log["audit"])


async def test_each_agent_keeps_its_own_schedule() -> None:
    """A security sweep and a dock review do not want the same cadence, and one shared period would be
    wrong for both."""
    import time

    frequent = StubAgent(name="frequent", interval_s=0.0)
    rare = StubAgent(name="rare", interval_s=3600.0)
    runner, _ = a_runner([frequent, rare])

    now = time.monotonic()
    assert runner.due(frequent, now)
    assert runner.due(rare, now)
    await runner.cycle(rare)
    assert not runner.due(rare, time.monotonic()), "it should not be due again for an hour"
    assert runner.due(frequent, time.monotonic())


async def test_the_proposal_reaches_the_human_with_its_reasoning() -> None:
    agent = StubAgent(proposal=a_proposal())
    runner, log = a_runner([agent])
    await runner.cycle(agent)

    audit_entry = next(item for item in log["audit"] if item["action"] == "propose")
    assert audit_entry["details"]["awaiting_approval"] is True
    assert "no events" in audit_entry["details"]["rationale"]


# ---------------------------------------------------------------------- memory
async def test_memory_is_offered_to_the_agent_before_it_reasons() -> None:
    precedent = MemoryEntry(
        memory_id="mem_1",
        agent="stub",
        situation="three trucks waiting",
        proposal="assign docks",
        outcome="rejected",
        reason="the docks were closed for maintenance",
        similarity=0.9,
    )
    agent = StubAgent(proposal=a_proposal())
    runner, _ = a_runner([agent], FakeMemory([precedent]))

    await runner.cycle(agent)

    assert agent.seen_memory is not None
    assert agent.seen_memory.found
    assert agent.seen_memory.rejections


def test_a_rejected_precedent_becomes_a_caution_on_the_proposal() -> None:
    """An agent that has had this proposal rejected must say so when proposing it again — the operator
    deserves the context, and repeating a rejected proposal silently is how automation earns distrust."""
    precedent = MemoryEntry(
        memory_id="mem_1",
        agent="stub",
        situation="s",
        proposal="p",
        outcome="rejected",
        reason="the docks were closed for maintenance",
        similarity=0.9,
    )
    recollection = Recollection(precedents=[precedent])
    proposal = a_proposal()
    proposal.memory = recollection

    text = proposal.with_memory_caution()
    assert "rejected 1 time(s) before" in text
    assert "maintenance" in text


def test_no_precedent_adds_no_caution() -> None:
    proposal = a_proposal()
    proposal.memory = Recollection()
    assert proposal.with_memory_caution() == proposal.rationale


def test_an_approved_precedent_is_not_a_caution() -> None:
    approved = MemoryEntry(
        memory_id="m", agent="a", situation="s", proposal="p", outcome="executed", similarity=0.8
    )
    assert Recollection(precedents=[approved]).caution is None


def test_a_dissimilar_hit_is_not_a_precedent() -> None:
    """A nearest-neighbour search always returns something. Treating its best hit as relevant regardless of
    distance is how an agent comes to "remember" an unrelated incident."""
    assert 0.0 < SIMILARITY_FLOOR < 1.0
    recollection = Recollection(precedents=[], searched=5, below_floor=5)
    assert not recollection.found
    assert recollection.below_floor == 5, "misses are reported as misses"
    assert recollection.describe()["below_floor"] == 5


async def test_memory_records_the_verdict_not_the_intention() -> None:
    """A memory written at proposal time records an intention. Counting an intention as a success is how an
    agent learns the wrong lesson."""
    memory = FakeMemory()
    agent = StubAgent(proposal=a_proposal())
    runner, _ = a_runner([agent], memory)

    result = await runner.cycle(agent)
    assert result.memory_entry is not None
    assert result.memory_entry.outcome == "pending", "no verdict yet"

    decision = a_decision(approval=ApprovalState.APPROVED)
    # The runner tracks entries by decision id; point it at the one just created.
    runner._entries[decision.decision_id] = result.memory_entry
    await runner.on_decision(decision)

    assert memory.outcomes, "the verdict must be recorded"
    assert memory.outcomes[-1][1] == "executed"


async def test_a_rejection_records_the_humans_reason() -> None:
    """The most valuable field in the memory by a wide margin."""
    memory = FakeMemory()
    entry = MemoryEntry(memory_id="mem_1", agent="stub", situation="s", proposal="p")
    runner, _ = a_runner([StubAgent()], memory)

    decision = a_decision(
        approval=ApprovalState.REJECTED,
        notes=["rejected by operator-jane: the docks were closed for maintenance"],
    )
    runner._entries[decision.decision_id] = entry
    await runner.on_decision(decision)

    assert memory.outcomes[-1][1] == "rejected"
    assert "maintenance" in (memory.outcomes[-1][2] or "")


async def test_memory_unavailable_degrades_to_nothing_not_to_a_keyword_match() -> None:
    """A keyword "precedent" would be a different thing wearing the same name, and an agent citing it would
    be misleading about why it acted."""
    memory = AgentMemory(None, None, tenant_id="acme")
    assert memory.unavailable
    recollection = await memory.recall("anything at all")
    assert not recollection.found
    assert recollection.precedents == []
    # And a write still returns an entry, so the loop does not have to special-case it.
    entry = await memory.remember(agent="a", situation="s", proposal="p")
    assert entry is not None and entry.outcome == "pending"


# ------------------------------------------------------------ concrete agents
async def test_the_security_agent_reports_when_it_cannot_see() -> None:
    """Silence from a broken sensor and silence from a quiet site look identical from here, and only one is
    fine. An agent that cannot see must say so rather than concluding all is well."""
    import httpx
    from sio_agents.agents import SecurityAgent

    class Broken:
        async def get(self, *args: Any, **kwargs: Any) -> Any:
            raise httpx.ConnectError("nothing is listening")

    observation = await SecurityAgent("http://x", "http://y", Broken()).observe()  # type: ignore[arg-type]
    assert not observation.interesting
    assert observation.why_not and "observation failed" in observation.why_not


async def test_the_logistics_agent_needs_both_a_queue_and_a_free_dock() -> None:
    """Trucks waiting while every dock is busy is a capacity problem an agent cannot fix, and proposing a
    schedule for it would be theatre."""
    from sio_agents.agents import LogisticsAgent

    class Responding:
        def __init__(self, trucks: int, free_docks: int) -> None:
            self.trucks = trucks
            self.free_docks = free_docks

        async def get(self, url: str, **kwargs: Any) -> Any:
            class Response:
                status_code = 200

                def __init__(self, payload: Any) -> None:
                    self._payload = payload

                def json(self) -> Any:
                    return self._payload

            if "entities" in url:
                return Response(
                    [
                        {
                            "entity_id": f"t{index}",
                            "label": f"Truck {index}",
                            "state": {"zone_id": "yard"},
                        }
                        for index in range(self.trucks)
                    ]
                )
            return Response(
                {
                    "zones": [
                        {
                            "zone_id": f"dock_{index}",
                            "kind": "dock",
                            "occupancy": 0 if index < self.free_docks else 1,
                        }
                        for index in range(4)
                    ]
                }
            )

        async def post(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("not used")

    agent = LogisticsAgent("http://x", "http://y", "http://z", Responding(5, 2))  # type: ignore[arg-type]
    busy = LogisticsAgent("http://x", "http://y", "http://z", Responding(5, 0))  # type: ignore[arg-type]
    quiet = LogisticsAgent("http://x", "http://y", "http://z", Responding(1, 3))  # type: ignore[arg-type]

    assert (await agent.observe()).interesting, "a queue with free docks is worth proposing"
    assert not (await busy.observe()).interesting, "no free dock means nothing to propose"
    assert not (await quiet.observe()).interesting, "a short queue is not a problem"


# -------------------------------------------------------------------- playbooks
def test_an_action_with_no_playbook_is_reported_not_substituted() -> None:
    """Substituting an action a human did not approve is the one thing this whole design exists to
    prevent."""
    from sio_agents.service import _playbook_for

    assert _playbook_for("dispatch_drone") == "IntrusionPlaybook"
    assert _playbook_for("close_gate") == "FireResponsePlaybook"
    assert _playbook_for("generate_report") is None
    assert _playbook_for("no_action") is None
    assert _playbook_for("something_invented") is None


async def test_an_approved_action_with_no_playbook_executes_nothing() -> None:
    runner, log = a_runner([StubAgent()])
    decision = a_decision(approval=ApprovalState.APPROVED, action=ActionType.GENERATE_REPORT)

    # The runner still calls execute; the SERVICE decides there is no playbook. Verified separately above,
    # so here the property is that the runner does not invent a substitute.
    outcome = await runner.on_decision(decision)
    assert outcome is not None
    assert len(log["executed"]) == 1
    assert log["executed"][0].chosen == decision.chosen


def test_the_runner_reports_the_gate_it_enforces() -> None:
    runner, _ = a_runner([StubAgent()])
    described = runner.describe()
    assert described["executions"] == 0
    assert "refused_executions" in described
    assert described["memory"]["similarity_floor"] == SIMILARITY_FLOOR
