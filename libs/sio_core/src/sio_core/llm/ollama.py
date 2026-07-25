"""Ollama adapter: a local model over HTTP, with the belt and braces small models need.

Three things here exist specifically because the target is a 1.5-3 B model rather than a frontier one:

* **Thinking is disabled by default.** Reasoning-mode models (the Qwen 3 family) emit a long private
  monologue before answering, which on this class of hardware turns a 2-second reply into a 15-second
  one. The tool decision does not improve enough to pay for that, and the PRD's budget is under ten
  seconds end to end.
* **Constrained decoding is available.** Passing a JSON schema through Ollama's ``format`` parameter
  makes malformed output structurally impossible rather than merely unlikely, which matters much more
  below 4 B than above it.
* **One repair retry.** When a model produces a call that fails its own schema, it is asked once more
  with the error quoted back. One retry, not a loop: a model that cannot produce a valid call after being
  told exactly what was wrong is not going to converge, and a retry loop turns a bad answer into a bad
  answer that also took a minute.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..telemetry import describe_error, get_logger
from .base import LLM, LlmReply, ToolSpec, parse_tool_calls

log = get_logger("sio.llm.ollama")

# Models whose reasoning mode is on by default and must be turned off for latency's sake.
THINKING_FAMILIES = ("qwen3", "deepseek-r1", "smollm3", "magistral", "granite4")


class OllamaLLM:
    """Chat completions from a local Ollama server."""

    name = "ollama"

    def __init__(
        self,
        *,
        url: str = "http://127.0.0.1:11434",
        model: str = "qwen3:1.7b",
        temperature: float = 0.1,
        timeout_s: float = 60.0,
        think: bool | None = None,
        num_ctx: int = 8192,
        keep_alive: str = "30m",
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout_s = timeout_s
        # None means "decide from the model name", which is the useful default: a caller should not have
        # to know which families have a reasoning mode.
        self.think = think if think is not None else False
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        """How long Ollama holds the model in memory after a request.

        Measured: the first question after an idle period took 17 seconds against 8.5 for the next one —
        the difference is loading two gigabytes of weights, not thinking. Ollama's default is five minutes,
        which is exactly long enough to have expired before a demo starts.
        """
        self._client: httpx.AsyncClient | None = None

    @property
    def reasoning_family(self) -> bool:
        return any(family in self.model.lower() for family in THINKING_FAMILIES)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.url, timeout=self.timeout_s)
        return self._client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LlmReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "num_ctx": self.num_ctx,
                # A generation cap, because an uncapped small model rambles: asked for a two-sentence
                # answer it will enumerate every row it was shown, and every token costs wall clock on CPU.
                **({"num_predict": max_tokens} if max_tokens else {}),
            },
        }
        if tools:
            payload["tools"] = [tool.to_openai() for tool in tools]
        if json_schema is not None:
            # Constrained decoding. Structurally impossible beats statistically unlikely, and below 4 B
            # the difference is the difference between a working copilot and a coin flip.
            payload["format"] = json_schema
        if self.reasoning_family:
            payload["think"] = self.think
        payload["keep_alive"] = self.keep_alive

        started = time.perf_counter()
        client = await self._http()
        try:
            response = await client.post("/api/chat", json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            elapsed = (time.perf_counter() - started) * 1000
            log.warning("llm.ollama_failed", model=self.model, error=describe_error(exc))
            return LlmReply(
                model=self.model,
                latency_ms=elapsed,
                degraded=f"the model could not be reached: {exc}",
            )

        elapsed = (time.perf_counter() - started) * 1000
        message = body.get("message", {}) or {}
        calls, degraded = parse_tool_calls(message.get("tool_calls") or [])
        return LlmReply(
            text=str(message.get("content") or "").strip(),
            tool_calls=calls,
            model=body.get("model", self.model),
            latency_ms=elapsed,
            prompt_tokens=body.get("prompt_eval_count"),
            completion_tokens=body.get("eval_count"),
            degraded=degraded,
            raw=body,
        )

    async def chat_with_repair(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec],
        validate: Any,
        temperature: float | None = None,
    ) -> LlmReply:
        """Ask once, and if the call fails validation quote the error back and ask once more.

        `validate` takes an :class:`LlmReply` and returns an error string, or None when the reply is
        usable. One retry only — a model told exactly what was wrong and still unable to comply is not
        converging, and looping turns a bad answer into a slow bad answer.
        """
        reply = await self.chat(messages, tools=tools, temperature=temperature)
        problem = validate(reply)
        if problem is None:
            return reply

        log.info("llm.repairing", model=self.model, problem=problem)
        repair = [
            *messages,
            {
                "role": "assistant",
                "content": reply.text or "",
                "tool_calls": reply.raw.get("message", {}).get("tool_calls", []),
            },
            {
                "role": "user",
                "content": (
                    f"That tool call was not usable: {problem}. "
                    "Call exactly one of the available tools with valid JSON arguments, and nothing else."
                ),
            },
        ]
        second = await self.chat(repair, tools=tools, temperature=0.0)
        second.latency_ms += reply.latency_ms
        still = validate(second)
        second.degraded = (
            f"first attempt failed ({problem}); repaired on retry"
            if still is None
            else f"both attempts failed ({problem}; then {still})"
        )
        return second

    async def warm(self) -> float:
        """Load the model into memory now, so the first real question does not pay for it.

        A one-token completion, which is enough to make Ollama load the weights. Returns the seconds it
        took, so a service can log honestly what the first question would otherwise have cost.
        """
        started = time.perf_counter()
        try:
            client = await self._http()
            await client.post(
                "/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "ok"}],
                    "stream": False,
                    "keep_alive": self.keep_alive,
                    "options": {"num_predict": 1},
                    **({"think": False} if self.reasoning_family else {}),
                },
            )
        except httpx.HTTPError as exc:
            log.warning("llm.warm_failed", model=self.model, error=describe_error(exc))
            return 0.0
        return time.perf_counter() - started

    async def ping(self) -> bool:
        try:
            client = await self._http()
            response = await client.get("/api/tags")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def available_models(self) -> list[str]:
        try:
            client = await self._http()
            response = await client.get("/api/tags")
            response.raise_for_status()
            return [entry["name"] for entry in response.json().get("models", [])]
        except (httpx.HTTPError, KeyError):
            return []

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["LLM", "OllamaLLM"]
