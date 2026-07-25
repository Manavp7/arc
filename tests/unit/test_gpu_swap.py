"""The GPU/production swap (PRD §9.3, Phase 7 P7.3).

The plan's acceptance is that `SIO_PROFILE=gpu` boots and **the same test suite passes at the seams** — so this
file is mostly parity: the same assertions run against the CPU adapter and the GPU one, against a mock
OpenAI-compatible server standing in for vLLM or NIM.

That distinction is the whole value of P7.3. A stub adapter whose parity with the CPU path has never been checked
is a *promise*, and the promise fails on the day somebody flips the profile in front of a customer. An adapter
that has been driven against a mock speaking the real wire protocol has at least been wrong in a place somebody
could see.

What a mock cannot tell you is also worth stating, because pretending otherwise is how this kind of test becomes
misleading: it does not prove Nemotron answers well, that a GPU is fast, or that DeepStream links against your
CUDA. It proves the *seam* holds — same reply shape, same tool extraction, same repair behaviour, same failure
reporting — which is the part this repository is responsible for.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sio_core.config import GPU_PROFILE, Settings
from sio_core.llm.base import LlmReply
from sio_core.llm.openai_compat import NO_TOOL_SUPPORT_MARKERS, OpenAICompatLLM
from sio_core.llm.scripted import ScriptedLLM


# --- the profile ------------------------------------------------------------------------------------
def test_the_cpu_profile_is_the_default(pristine_env: None) -> None:
    """`just check` runs on it, and a laptop is the common case."""
    assert Settings(_env_file=None).profile == "cpu"
    assert Settings(_env_file=None).bus_backend == "redis"


def test_the_gpu_profile_flips_every_seam(pristine_env: None) -> None:
    """One flip instead of eleven.

    Nobody swaps all of these individually — "run this on GPUs" is a single intention, and making somebody
    express it as eleven environment variables guarantees one gets missed and the half-swapped result is blamed
    on the platform.
    """
    settings = Settings(_env_file=None, profile="gpu")
    for field, expected in GPU_PROFILE.items():
        assert getattr(settings, field) == expected, f"{field} did not follow the profile"


def test_a_deliberately_changed_seam_survives_the_profile(pristine_env: None) -> None:
    """`SIO_PROFILE=gpu SIO_LLM_PROVIDER=scripted` has to mean what it says.

    That combination is precisely how somebody tests GPU wiring on a laptop with no GPU, and a profile that
    overrode it would be a trap — a silent one, since the operator could not tell which of their two settings
    won.
    """
    settings = Settings(_env_file=None, profile="gpu", llm_provider="scripted", detector="onnx")
    assert settings.llm_provider == "scripted"
    assert settings.detector == "onnx"
    # The rest still follows.
    assert settings.bus_backend == "kafka"


def test_the_profile_rule_is_default_comparison_not_fields_set(pristine_env: None) -> None:
    """The bug that cost a debugging session, pinned.

    This repository ships a `.env` listing EVERY field with its default value, as documentation. So
    `model_fields_set` contains all ~130 fields on every run, and a profile gated on "was this supplied?" could
    never apply anything. Pydantic was reporting the truth; the truth was not the question I meant to ask.

    Asserted through behaviour: a `Settings` built with every profile field explicitly passed at its default
    must still take the profile, which is only true under the default-comparison rule.
    """
    at_defaults = {field: Settings.model_fields[field].default for field in GPU_PROFILE}
    settings = Settings(_env_file=None, profile="gpu", **at_defaults)
    assert settings.bus_backend == "kafka", (
        "the profile did not apply to a field passed at its default — the rule has regressed to fields_set"
    )


def test_every_profile_value_is_legal_for_its_seam() -> None:
    """A typo in the profile table becomes a test failure rather than a runtime surprise on a cluster.

    `Settings` validates its Literals, so constructing with each value individually is the check.
    """
    for field, value in GPU_PROFILE.items():
        Settings(**{field: value})  # raises if the value is not in its Literal


def test_the_gpu_profile_is_coherent_not_merely_selected(pristine_env: None) -> None:
    """It must produce a configuration that could actually work.

    Without a default endpoint the base URL stays empty, the adapter builds `/v1`, and the profile boots into
    something that cannot function — which is worse than having no profile, because it looks configured.
    """
    settings = Settings(_env_file=None, profile="gpu")
    assert settings.openai_base_url.startswith("http")
    assert settings.openai_base_url.endswith("/v1")


def test_the_adapter_summary_reports_the_profile(pristine_env: None) -> None:
    """`/health` has to say which world it is in, or a swap is invisible in production."""
    summary = Settings(_env_file=None, profile="gpu").adapter_summary()
    assert summary["profile"] == "gpu"
    assert summary["bus"] == "kafka"


# --- the OpenAI-compatible adapter, against a mock server -------------------------------------------
class MockOpenAIServer:
    """A stand-in for vLLM / NIM / TGI, speaking the real wire shape.

    Deliberately returns OpenAI's exact response envelope rather than a convenient one, including the detail
    that matters most: `tool_calls[].function.arguments` is a JSON **string**, not an object. Ollama returns it
    as an object. If the adapter did not route both through the shared `parse_tool_calls`, that difference would
    be a live bug the day somebody flipped the profile.
    """

    def __init__(
        self,
        *,
        content: str = "There are 12 trucks on site.",
        tool_calls: list[dict[str, Any]] | None = None,
        status: int = 200,
        body_override: dict[str, Any] | None = None,
        error_text: str = "",
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.status = status
        self.body_override = body_override
        self.error_text = error_text
        self.requests: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict[str, Any] | None = None) -> Any:
        self.requests.append({"url": url, "body": json or {}})
        if self.status >= 400:
            return MockResponse(self.status, text=self.error_text or "error")
        if self.body_override is not None:
            return MockResponse(200, body=self.body_override)
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls is not None:
            message["tool_calls"] = self.tool_calls
        return MockResponse(
            200,
            body={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "model": "nvidia/nemotron-3-8b-chat",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59},
            },
        )

    async def get(self, url: str, timeout: float | None = None) -> Any:  # noqa: ASYNC109 - mirrors httpx
        if "/models" in url:
            return MockResponse(200, body={"data": [{"id": "nvidia/nemotron-3-8b-chat"}]})
        return MockResponse(404, text="not found")

    async def aclose(self) -> None:
        return None


class MockResponse:
    def __init__(self, status: int, body: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status
        self._body = body
        self.text = text or json.dumps(body or {})

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body


def a_client(server: MockOpenAIServer, **kwargs: Any) -> OpenAICompatLLM:
    client = OpenAICompatLLM(url="http://mock:8001/v1", **kwargs)
    client._client = server
    return client


@pytest.mark.asyncio
async def test_a_plain_answer_comes_back_in_the_shared_reply_shape() -> None:
    server = MockOpenAIServer(content="There are 12 trucks on site.")
    reply = await a_client(server).chat([{"role": "user", "content": "how many trucks?"}])

    assert isinstance(reply, LlmReply)
    assert reply.text == "There are 12 trucks on site."
    assert reply.model == "nvidia/nemotron-3-8b-chat"
    assert reply.degraded is None
    assert reply.prompt_tokens == 42
    assert reply.completion_tokens == 17
    assert reply.latency_ms >= 0


@pytest.mark.asyncio
async def test_tool_arguments_arriving_as_a_json_string_are_repaired() -> None:
    """The single most important parity case.

    OpenAI-compatible servers send `arguments` as a JSON **string** by specification; Ollama sends an object.
    Both go through the shared `parse_tool_calls`, which already repairs the string form because small local
    models produce it by accident. If the adapter had its own extractor, this difference would be a live bug on
    the day somebody flipped the profile — and it would present as a copilot that answers fluently without ever
    calling a tool.
    """
    server = MockOpenAIServer(
        content="",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "list_entities",
                    # A STRING, which is what the specification says.
                    "arguments": '{"type": "truck", "zone_id": "dock_3"}',
                },
            }
        ],
    )
    reply = await a_client(server).chat([{"role": "user", "content": "trucks in dock 3?"}])

    assert len(reply.tool_calls) == 1
    call = reply.tool_calls[0]
    assert call.name == "list_entities"
    # Parsed into a real object, not left as a string for every caller to json.loads.
    assert call.arguments == {"type": "truck", "zone_id": "dock_3"}


@pytest.mark.asyncio
async def test_the_same_extractor_handles_both_adapters_shapes() -> None:
    """Parity, asserted directly rather than inferred.

    Ollama's object-shaped arguments and OpenAI's string-shaped ones must produce identical `ToolCall`s, because
    everything downstream — the copilot's tool loop, the explanation, the audit record — reads only that.
    """
    from sio_core.llm.base import parse_tool_calls

    openai_shape = [{"function": {"name": "list_entities", "arguments": '{"type": "truck"}'}}]
    ollama_shape = [{"function": {"name": "list_entities", "arguments": {"type": "truck"}}}]

    from_openai, _ = parse_tool_calls(openai_shape)
    from_ollama, _ = parse_tool_calls(ollama_shape)

    assert from_openai[0].name == from_ollama[0].name
    assert from_openai[0].arguments == from_ollama[0].arguments


@pytest.mark.asyncio
async def test_an_unreachable_server_is_degraded_not_raised() -> None:
    """Matching the Ollama adapter exactly.

    A copilot that raises when its model is down is one that takes the API with it; a copilot that reports
    `degraded` still answers what it can from context, and says so.
    """

    class Dead:
        async def post(self, *_: Any, **__: Any) -> Any:
            raise ConnectionError("connection refused")

    client = a_client(MockOpenAIServer())
    client._client = Dead()
    reply = await client.chat([{"role": "user", "content": "hello"}])

    assert reply.text == ""
    assert reply.degraded is not None
    # Named for what it is: "the server is unreachable" and "the model is broken" send an operator to
    # completely different places.
    assert "unreachable" in reply.degraded


@pytest.mark.asyncio
async def test_a_server_error_carries_the_body() -> None:
    server = MockOpenAIServer(status=500, error_text="CUDA out of memory")
    reply = await a_client(server).chat([{"role": "user", "content": "hello"}])
    assert reply.degraded is not None
    # The body, not just the code. "CUDA out of memory" is actionable; "500" is not.
    assert "CUDA out of memory" in reply.degraded


@pytest.mark.asyncio
async def test_a_200_with_no_choices_is_an_error_not_an_empty_answer() -> None:
    """Some proxies return this when a backend is draining.

    An empty answer looks like the model having nothing to say, which is a completely different fact.
    """
    server = MockOpenAIServer(body_override={"choices": [], "model": "x"})
    reply = await a_client(server).chat([{"role": "user", "content": "hello"}])
    assert reply.degraded is not None
    assert "draining" in reply.degraded


@pytest.mark.asyncio
async def test_a_server_without_tool_support_is_detected_and_retried_once() -> None:
    """llama.cpp ignores `tools`; TGI returns 422.

    Passing tools to a server that cannot use them and receiving fluent prose is worse than being told no, so
    the refusal is detected and the request retried without them — once, and remembered, because it is a fact
    about the deployment rather than about the request.
    """

    class RefusesTools(MockOpenAIServer):
        def __init__(self) -> None:
            super().__init__(content="I cannot check that.")
            self.attempts = 0

        async def post(self, url: str, json: dict[str, Any] | None = None) -> Any:
            self.attempts += 1
            if "tools" in (json or {}):
                return MockResponse(400, text="tools is not supported by this server")
            return await super().post(url, json)

    server = RefusesTools()
    client = a_client(server)
    tools = [{"name": "list_entities", "description": "list", "parameters": {}}]
    reply = await client.chat([{"role": "user", "content": "hi"}], tools=tools)

    assert reply.text == "I cannot check that."
    assert server.attempts == 2, "it should retry exactly once without tools"
    # Remembered, so the next turn does not pay the same double latency.
    assert client._tools_unsupported is True

    server.attempts = 0
    await client.chat([{"role": "user", "content": "again"}], tools=tools)
    assert server.attempts == 1, "the second turn must not re-learn that tools are unsupported"


@pytest.mark.parametrize("marker", NO_TOOL_SUPPORT_MARKERS)
def test_the_no_tool_markers_are_lowercase(marker: str) -> None:
    """They are matched against a lowercased body; an uppercase marker would never match."""
    assert marker == marker.lower()


@pytest.mark.asyncio
async def test_temperature_is_only_sent_when_asked_for() -> None:
    """An operator who tuned their vLLM launch flags did not expect a client to second-guess them."""
    server = MockOpenAIServer()
    await a_client(server).chat([{"role": "user", "content": "hi"}])
    assert "temperature" not in server.requests[0]["body"]

    await a_client(server).chat([{"role": "user", "content": "hi"}], temperature=0.1)
    assert server.requests[1]["body"]["temperature"] == 0.1


@pytest.mark.asyncio
async def test_tools_are_sent_in_openai_function_shape() -> None:
    server = MockOpenAIServer()
    await a_client(server).chat(
        [{"role": "user", "content": "hi"}],
        tools=[
            {"name": "list_entities", "description": "list them", "parameters": {"type": "object"}}
        ],
    )
    sent = server.requests[0]["body"]["tools"][0]
    assert sent["type"] == "function"
    assert sent["function"]["name"] == "list_entities"
    assert server.requests[0]["body"]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_available_models_answers_the_commonest_swap_mistake() -> None:
    """A model name that does not match what vLLM loaded.

    The 404 body says "model not found" without saying what *would* be found, so the adapter can ask.
    """
    server = MockOpenAIServer()
    assert await a_client(server).available_models() == ["nvidia/nemotron-3-8b-chat"]


@pytest.mark.asyncio
async def test_ping_does_not_occupy_the_gpu() -> None:
    """`/v1/models`, not a completion.

    Every compatible server implements it, and a health check should not cost a queue slot on a busy instance.
    """
    server = MockOpenAIServer()
    assert await a_client(server).ping() is True
    assert not server.requests, "ping must not post a completion"


def test_the_base_url_is_tolerant_of_both_forms() -> None:
    """Half the deployment guides write it with `/v1` and half without.

    A 404 from a path built by string concatenation is a miserable way to learn which.
    """
    assert OpenAICompatLLM(url="http://x:8001").url == "http://x:8001/v1"
    assert OpenAICompatLLM(url="http://x:8001/v1").url == "http://x:8001/v1"
    assert OpenAICompatLLM(url="http://x:8001/v1/").url == "http://x:8001/v1"


def test_an_empty_base_url_is_refused_with_a_pointer() -> None:
    """Rather than silently building `/v1` and 404ing at the first question somebody asks."""
    with pytest.raises(ValueError, match="SIO_OPENAI_BASE_URL"):
        OpenAICompatLLM(url="")


def test_the_timeout_is_generous_because_a_shared_endpoint_queues() -> None:
    """A vLLM instance under load accepts the request and holds it.

    A 30-second timeout against a busy endpoint produces a copilot that fails under exactly the load it was
    bought to handle.
    """
    from sio_core.llm.openai_compat import DEFAULT_TIMEOUT_S

    assert DEFAULT_TIMEOUT_S >= 60


# --- the seam itself --------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_both_llm_adapters_satisfy_the_same_protocol() -> None:
    """The claim a seam makes, asserted rather than annotated.

    Every adapter must expose the same surface, because the copilot's tool loop calls exactly these and nothing
    else. A `Protocol` is not checked at runtime, so without this the annotation is a comment.
    """
    for adapter in (OpenAICompatLLM(url="http://x/v1"), ScriptedLLM()):
        for attribute in ("name", "model", "chat", "ping", "close"):
            assert hasattr(adapter, attribute), f"{type(adapter).__name__} is missing {attribute}"


def test_the_registry_selects_the_compat_adapter_under_the_gpu_profile(pristine_env: None) -> None:
    """The end-to-end of the swap: one environment variable, a different client."""
    from sio_core import get_llm

    llm = get_llm(Settings(_env_file=None, profile="gpu"))
    assert type(llm).__name__ == "OpenAICompatLLM"
    assert llm.url == "http://127.0.0.1:8001/v1"


def test_the_registry_still_selects_ollama_by_default(pristine_env: None) -> None:
    from sio_core import get_llm

    assert type(get_llm(Settings(_env_file=None))).__name__ == "OllamaLLM"


# --- the stubs ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(__import__("sio_core.stubs", fromlist=["STUBS"]).STUBS))
def test_every_stub_refuses_rather_than_silently_doing_nothing(name: str) -> None:
    """The property that makes a stub safe.

    An adapter that accepts work and discards it is the worst possible component in a pipeline: a bus that
    acknowledges publishes into nothing produces a system where every counter is healthy and no data exists. So
    every operation raises, and the message names what to install, what changes and what to use instead.

    Parametrised over the registry rather than listing the five, so a stub added later is covered without
    anybody remembering to add it here.
    """
    from sio_core.errors import ConfigError
    from sio_core.stubs import STUBS

    stub = STUBS[name]()
    for operation in ("publish", "search", "detect", "forecast", "anything_at_all"):
        with pytest.raises(ConfigError) as caught:
            getattr(stub, operation)()
        message = str(caught.value)
        assert operation in message
        assert "docs/GPU_SWAP.md" in message
        # The three things somebody needs: what it wants, what it changes, what to do now.
        assert "needs:" in message
        assert "changes:" in message
        assert "instead:" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(__import__("sio_core.stubs", fromlist=["STUBS"]).STUBS))
async def test_a_stub_ping_returns_false_rather_than_raising(name: str) -> None:
    """`ping` is the one exception to refusing.

    Health checks call it on a loop, and an exception there takes down the endpoint whose job is to report the
    problem. False is the honest answer: not reachable.
    """
    from sio_core.stubs import STUBS

    stub = STUBS[name]()
    assert await stub.ping() is False
    await stub.close()  # closing something never opened must not be an error path


def test_a_stub_describes_itself_for_doctor() -> None:
    from sio_core.stubs import KafkaBusStub

    described = KafkaBusStub().describe()
    assert described["status"] == "wired, not implemented here"
    assert "redis" in described["fallback"]


def test_the_whole_gpu_profile_constructs(pristine_env: None) -> None:
    """The plan's acceptance: `SIO_PROFILE=gpu` BOOTS.

    Every seam resolves to something — real where it could be verified here, an honest stub where it could not.
    A profile that raised on construction could not be inspected by `just doctor` before the hardware arrives,
    which is exactly when somebody wants to look at it.
    """
    from sio_core import get_bus, get_graph, get_vectors

    settings = Settings(_env_file=None, profile="gpu")
    assert type(get_bus(settings)).__name__ == "KafkaBusStub"
    assert type(get_vectors(settings)).__name__ == "QdrantVectorStoreStub"
    # Memgraph is REAL: it speaks Bolt and Cypher, so the Neo4j adapter reaches it unchanged. The only seam in
    # the profile that was free.
    assert type(get_graph(settings)).__name__ == "Neo4jGraphStore"


def test_memgraph_is_a_legal_graph_backend() -> None:
    """Because the profile selects it, and a Literal that does not include it would fail at load."""
    assert Settings(_env_file=None, graph_backend="memgraph").graph_backend == "memgraph"


def test_every_stub_documents_what_using_it_would_change() -> None:
    """An adapter listed as "coming later" with no statement of consequence is a name, not a plan.

    The `changes` field is what tells somebody whether they need it, and it is asserted non-trivial so a future
    stub cannot be added with an empty one.
    """
    from sio_core.stubs import STUBS

    for name, stub_class in STUBS.items():
        assert len(stub_class.changes) > 40, f"{name} does not say what it would change"
        assert stub_class.requires, f"{name} does not say what it needs"
        assert stub_class.fallback, f"{name} does not say what to use instead"
