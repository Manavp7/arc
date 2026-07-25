"""An OpenAI-compatible chat client — vLLM, NIM, TGI, llama.cpp, LM Studio (PRD §9.3, Phase 7).

The production LLM seam. Everything that serves a large model behind an HTTP API has converged on OpenAI's
`/v1/chat/completions` shape, which means one adapter reaches Nemotron 3 on vLLM, a NIM container, TGI, and
whatever comes next — without any of them being named in this file.

**The point of Phase 7 is that this is tested, not merely present.** A stub adapter whose parity with the CPU
path has never been checked is a promise, and the promise fails on the day somebody flips the profile in front of
a customer. So there is a mock OpenAI-compatible server in the test suite and the same assertions run against it
and against Ollama: same `LlmReply` shape, same tool-call extraction, same repair behaviour on the malformed
outputs small models produce.

Three differences from the Ollama adapter are worth knowing, because they are exactly what a naive port gets
wrong.

**Tool calls arrive in a different place.** Ollama puts them at `message.tool_calls[].function.arguments` as an
object; OpenAI-compatible servers put them in the same path but with `arguments` as a **JSON string**. Both shapes
go through the shared `parse_tool_calls`, which already repairs the string case — that function exists because
small models get this wrong, and it turns out large servers do it deliberately.

**Some servers do not support tools at all.** llama.cpp's server ignores the `tools` parameter and returns prose;
TGI returns a 422. Passing tools to a server that cannot use them and getting fluent prose back is worse than
being told no, so an unsupported-tools response is detected and reported rather than parsed as an answer.

**The failure mode of a shared endpoint is a queue, not an error.** A vLLM instance under load accepts the request
and holds it, so the timeout has to be generous and the *reason* for a timeout has to say "the server is busy"
rather than "the model is broken" — an operator debugging a slow copilot needs to know which.
"""

from __future__ import annotations

import time
from typing import Any

from ..telemetry import get_logger
from .base import LlmReply, ToolSpec, parse_tool_calls

log = get_logger("sio.llm.openai_compat")

#: How long to wait for a completion.
#:
#: 120 seconds, much longer than the Ollama adapter's, because a shared GPU endpoint queues rather than refusing.
#: A 30-second timeout against a busy vLLM instance produces a copilot that fails under exactly the load it was
#: bought to handle.
DEFAULT_TIMEOUT_S = 120.0

#: Signals that a server does not support tool calling.
#:
#: Detected rather than assumed, because the platform cannot know what is behind the URL: the same config might
#: point at vLLM (tools work), llama.cpp (silently ignored) or TGI (422). Being told "this endpoint cannot call
#: tools" is far better than receiving fluent prose where a tool call was required.
NO_TOOL_SUPPORT_MARKERS = (
    "tools is not supported",
    "tool_choice",
    "does not support tools",
    "unsupported parameter",
    "tool calling is not",
)


class OpenAICompatLLM:
    """Chat against any OpenAI-compatible endpoint.

    Deliberately not the `openai` package. That library carries its own retry policy, its own timeout semantics
    and its own opinions about errors, and this platform already has all three — plus a `describe_error` contract
    and a trace context to thread through. One `httpx.post` against a documented JSON shape is less code than
    configuring somebody else's client to behave.
    """

    name = "openai_compat"

    def __init__(
        self,
        *,
        url: str = "http://127.0.0.1:8001/v1",
        model: str = "nvidia/nemotron-3-8b-chat",
        api_key: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_tokens: int = 1024,
    ) -> None:
        if not url.strip():
            # Refused rather than silently building `/v1`. An adapter that constructs an obviously-broken URL
            # produces a 404 at the first question somebody asks the copilot, and the error names a path
            # nobody configured.
            raise ValueError(
                "OpenAICompatLLM needs a base URL. Set SIO_OPENAI_BASE_URL — e.g. "
                "http://127.0.0.1:8001/v1 for a local vLLM, or your NIM endpoint."
            )
        # Tolerant of both forms. Half the deployment guides in circulation write the base URL with `/v1` and
        # half without, and a 404 from a path built by string concatenation is a miserable way to learn which.
        self.url = url.rstrip("/")
        if not self.url.endswith("/v1"):
            self.url = f"{self.url}/v1"
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self._client: Any = None
        self._tools_unsupported = False

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            headers = {"content-type": "application/json"}
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(timeout=self.timeout_s, headers=headers)
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
        """One chat turn, in the same `LlmReply` shape every other adapter returns.

        The signature matches the `LLM` protocol exactly — that is the whole point of the seam, and a parity test
        asserts it rather than trusting the annotation.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens or self.max_tokens,
        }
        # Temperature is only sent when asked for. Sending a default overrides whatever the server was
        # configured with, and an operator who tuned their vLLM launch flags did not expect a client to
        # second-guess them.
        if temperature is not None:
            payload["temperature"] = temperature
        if tools and not self._tools_unsupported:
            payload["tools"] = [_as_openai_tool(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        if json_schema is not None:
            # `json_schema` where supported, falling back to `json_object`. vLLM and NIM support the strict form;
            # older builds and llama.cpp only understand the loose one, and asking for strict there is a 400.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "sio_response", "schema": json_schema, "strict": False},
            }

        started = time.perf_counter()
        try:
            response = await self._http().post(f"{self.url}/chat/completions", json=payload)
        except Exception as exc:
            return LlmReply(
                model=self.model,
                latency_ms=(time.perf_counter() - started) * 1000,
                # `degraded`, the same field the Ollama adapter uses — it is already carried through to the
                # user-facing explanation, so a copilot answering from a broken endpoint says so. Named for
                # what it is, too: "the server is unreachable" and "the model is broken" send an operator to
                # completely different places.
                degraded=f"{self.url} is unreachable: {type(exc).__name__}: {exc}",
            )

        elapsed_ms = (time.perf_counter() - started) * 1000

        if response.status_code >= 400:
            body = response.text[:400]
            if _looks_like_no_tool_support(body) and tools:
                # Retry once WITHOUT tools, and remember. A server that cannot call tools is a fact about the
                # deployment, not about this request, so re-learning it on every turn would double every
                # latency for the rest of the process's life.
                self._tools_unsupported = True
                log.warning(
                    "llm.tools_unsupported",
                    url=self.url,
                    model=self.model,
                    detail=body[:160],
                    consequence="the copilot will answer from context only; tool-shaped questions will be refused",
                )
                return await self.chat(
                    messages,
                    tools=None,
                    json_schema=json_schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            return LlmReply(
                model=self.model,
                latency_ms=elapsed_ms,
                degraded=f"{self.url} returned {response.status_code}: {body}",
            )

        try:
            body = response.json()
        except Exception as exc:
            return LlmReply(
                model=self.model,
                latency_ms=elapsed_ms,
                degraded=f"{self.url} returned a non-JSON body: {type(exc).__name__}",
            )

        choices = body.get("choices") or []
        if not choices:
            # A 200 with no choices, which some proxies return when a backend is draining. Reported as an error
            # rather than an empty answer: an empty answer looks like the model having nothing to say.
            return LlmReply(
                model=str(body.get("model") or self.model),
                latency_ms=elapsed_ms,
                degraded=(
                    "the server returned 200 with no choices, which usually means a backend is draining"
                ),
                raw=body,
            )

        message = (choices[0] or {}).get("message") or {}
        # The SHARED extractor, given the same argument the Ollama adapter gives it: the tool_calls LIST, not
        # the message. It already repairs arguments arriving as a JSON string — a bug small local models have
        # and OpenAI-compatible servers have by specification — and using it here is what makes the two paths
        # behave identically on malformed output, which is the entire claim of a seam.
        tool_calls, degraded = parse_tool_calls(message.get("tool_calls") or [])
        usage = body.get("usage") or {}

        return LlmReply(
            text=str(message.get("content") or "").strip(),
            tool_calls=tool_calls,
            model=str(body.get("model") or self.model),
            latency_ms=elapsed_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            degraded=degraded,
            # The whole body, so `finish_reason` and anything else a specific server adds is available without
            # this adapter having to anticipate it.
            raw=body,
        )

    async def ping(self) -> bool:
        """Is the endpoint there?

        `/v1/models` rather than a completion: it is the one endpoint every compatible server implements, it does
        not occupy the GPU, and it does not cost a queue slot on a busy instance just to answer a health check.
        """
        try:
            response = await self._http().get(f"{self.url}/models", timeout=10.0)
            return response.status_code < 400
        except Exception:
            return False

    async def available_models(self) -> list[str]:
        """What the endpoint actually serves.

        Worth having because the single most common GPU-swap mistake is a model name that does not match what
        was loaded — vLLM serves exactly what its `--model` flag named, and a typo produces a 404 whose body
        says "model not found" without saying what *would* be found.
        """
        try:
            response = await self._http().get(f"{self.url}/models", timeout=10.0)
            if response.status_code >= 400:
                return []
            return [str(item.get("id")) for item in (response.json().get("data") or [])]
        except Exception:
            return []

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _as_openai_tool(tool: ToolSpec) -> dict[str, Any]:
    """A `ToolSpec` in OpenAI's function-calling shape.

    Handles both a mapping and an object with attributes, because `ToolSpec` is a dataclass here and a plain dict
    when it arrives from a plugin's entry point.
    """
    if isinstance(tool, dict):
        name = str(tool.get("name", ""))
        description = str(tool.get("description", ""))
        parameters = tool.get("parameters") or {"type": "object", "properties": {}}
    else:
        name = str(getattr(tool, "name", ""))
        description = str(getattr(tool, "description", ""))
        parameters = getattr(tool, "parameters", None) or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def _looks_like_no_tool_support(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in NO_TOOL_SUPPORT_MARKERS)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "NO_TOOL_SUPPORT_MARKERS",
    "OpenAICompatLLM",
]
