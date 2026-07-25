"""Copilot service: natural-language questions answered with citations (PRD M13, M20)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from sio_core import SioService, get_graph, get_llm
from sio_core.llm import ScriptedLLM

from .agent import CopilotAgent
from .evalset import EVAL_CASES, scripted_routes
from .tools import ToolBelt


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class CopilotService(SioService):
    """Answers questions about the world model, and shows its working.

    Subscribes to nothing. The copilot is request-driven and reads through the same API surface an
    external client would — so it has no bus state to keep, and nothing it does depends on having been
    running when an event arrived.
    """

    name = "copilot"
    subscribes = ()
    # A tick exists solely to keep the model resident. Ollama unloads after a period of inactivity, and an
    # operator who asks one question an hour would pay the load cost every single time.
    tick_interval_s = 600.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.llm = get_llm(self.settings)
        if isinstance(self.llm, ScriptedLLM) and not self.llm.routes:
            # A scripted model with no script answers nothing. Loading the eval routes makes the scripted
            # provider genuinely useful — it is the demo path when no model is installed, not just a test
            # double.
            self.llm.routes = scripted_routes()
        self.belt = ToolBelt(
            api_url=f"http://127.0.0.1:{self.settings.api_port}",
            spatial_url=f"http://127.0.0.1:{self.settings.spatial_port}",
            prediction_url=f"http://127.0.0.1:{self.settings.prediction_port}",
            worldmodel_url=f"http://127.0.0.1:{self.settings.worldmodel_port}",
            ingest_url=f"http://127.0.0.1:{self.settings.ingest_port}",
            graph=get_graph(self.settings),
            tenant_id=self.settings.tenant_id,
        )
        self.agent = CopilotAgent(self.llm, self.belt, max_steps=self.settings.llm_max_tool_steps)
        self._asked = 0
        self._total_ms = 0.0
        self._slowest_ms = 0.0

    async def setup(self) -> None:
        reachable = await self.llm.ping()
        if reachable:
            warmed = await self._warm()
            self.log.info(
                "copilot.model_warm", seconds=round(warmed, 2), model=getattr(self.llm, "model", "")
            )
        self.log.info(
            "copilot.ready",
            provider=self.llm.name,
            model=getattr(self.llm, "model", "n/a"),
            reachable=reachable,
            tools=[tool.name for tool in self.belt.tools()],
            eval_cases=len(EVAL_CASES),
        )
        if not reachable:
            self.log.warning(
                "copilot.model_unreachable",
                effect="questions will fall back to deterministic keyword routing",
                hint=f"start ollama and pull {getattr(self.llm, 'model', '')}",
            )

    async def _warm(self) -> float:
        """Warm the model with THE REAL PROMPT AND TOOLS, not a bare token.

        The first version sent a one-token completion with no tools. It loaded the weights, and the first
        real question still took 16 s against 9 s for the next — because Ollama caches the processed prompt
        PREFIX, and the prefix that matters is the system prompt plus nine tool schemas, about fifteen
        hundred tokens of JSON. Warming a different prefix warms the wrong thing.

        So the warm-up is a real request, shaped exactly like production's, capped to one output token.
        """
        started = time.perf_counter()
        from .agent import SYSTEM_PROMPT

        try:
            await self.llm.chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "ready?"},
                ],
                tools=self.belt.specs(),
                max_tokens=1,
            )
        except Exception as exc:
            self.log.warning("copilot.warm_failed", error=str(exc))
        return time.perf_counter() - started

    async def tick(self) -> None:
        """Keep the model resident and its prompt prefix cached."""
        await self._warm()

    async def health_checks(self) -> dict[str, str]:
        reachable = await self.llm.ping()
        return {
            "llm": f"ok ({self.llm.name}: {getattr(self.llm, 'model', 'n/a')})"
            if reachable
            else "degraded: model unreachable, answers will use keyword routing",
            "graph": "ok" if self.belt.graph is not None else "no graph store",
        }

    async def health_info(self) -> dict[str, str]:
        return {
            "asked": str(self._asked),
            "mean_ms": f"{self._total_ms / self._asked:.0f}" if self._asked else "0",
            "slowest_ms": f"{self._slowest_ms:.0f}",
            "fallbacks": str(self.agent.fallbacks),
            "tool_calls": str(self.belt.calls),
            "tool_failures": str(self.belt.failures),
        }

    async def teardown(self) -> None:
        await self.belt.close()
        await self.llm.close()

    def routes(self, app: FastAPI) -> None:
        @app.post("/copilot/ask", tags=["copilot"])
        async def ask(request: AskRequest) -> dict[str, Any]:
            """Answer a question, with the evidence and the reasoning attached."""
            started = time.perf_counter()
            answer = await self.agent.ask(request.question)
            elapsed = (time.perf_counter() - started) * 1000
            self._asked += 1
            self._total_ms += elapsed
            self._slowest_ms = max(self._slowest_ms, elapsed)
            return {
                "question": request.question,
                "answer": answer.text,
                "confidence": answer.confidence,
                "explanation": answer.explanation.to_wire(),
                "trace": answer.trace.describe(),
                "elapsed_ms": round(elapsed, 1),
            }

        @app.get("/copilot/tools", tags=["copilot"])
        async def tools() -> dict[str, Any]:
            """The tools offered to the model, exactly as the model sees them."""
            return {
                "tools": [
                    {
                        "name": tool.spec.name,
                        "description": tool.spec.description,
                        "parameters": tool.spec.parameters,
                    }
                    for tool in self.belt.tools()
                ]
            }

        @app.get("/copilot/evalset", tags=["copilot"])
        async def evalset() -> dict[str, Any]:
            """The evaluation set: the questions this copilot is held to.

            Exposed rather than buried in the test suite, because it is the honest answer to "what can it
            do?" — and because a claim about a model's tool-calling score means nothing without the
            fixture it was scored on.
            """
            return {
                "cases": [
                    {
                        "id": case.id,
                        "question": case.question,
                        "expect_tool": case.expect_tool,
                        "acceptable_tools": list(case.acceptable_tools),
                        "use_case": case.use_case,
                        "note": case.note,
                    }
                    for case in EVAL_CASES
                ],
                "count": len(EVAL_CASES),
                "restraint_cases": sum(1 for case in EVAL_CASES if case.expect_tool is None),
            }

        @app.post("/copilot/eval", tags=["copilot"])
        async def run_eval(limit: int = 0) -> dict[str, Any]:
            """Run the eval set against the configured model and report tool-selection accuracy.

            The same measurement `scripts/eval_tool_calling.py` performs offline, available live so a
            deployment can be checked against the model it actually has rather than the one it was tested
            with.
            """
            cases = EVAL_CASES[: limit or len(EVAL_CASES)]
            results = []
            correct = 0
            for case in cases:
                answer = await self.agent.ask(case.question)
                chosen = answer.trace.tools_used[0] if answer.trace.tools_used else None
                ok = chosen == case.expect_tool or (
                    chosen in case.acceptable_tools if chosen else False
                )
                correct += int(ok)
                results.append(
                    {
                        "id": case.id,
                        "expected": case.expect_tool,
                        "chose": chosen,
                        "ok": ok,
                        "fallback": answer.trace.used_fallback,
                        "ms": round(answer.trace.total_ms, 1),
                    }
                )
            return {
                "model": getattr(self.llm, "model", "n/a"),
                "cases": len(cases),
                "correct": correct,
                "accuracy": round(correct / max(1, len(cases)), 3),
                "results": results,
            }


__all__ = ["CopilotService"]
