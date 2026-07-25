"""The Python SDK (PRD M22, Phase 6).

The acceptance is "the quickstart in docs/SDK.md runs", so the first thing tested is that the documented
quickstart and the runnable script cannot drift apart — a quickstart pasted into prose stops working the first
time an API changes while still looking correct, which is worse than not having one because it is the first thing
a new user tries.

The rest pins the four things an SDK exists to absorb: tokens, typed returns, streaming, and errors that say what
to do.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from sio_sdk import CopilotAnswer, SioApiError, SioClient, StreamMessage, SyncSioClient
from sio_sdk.client import RENEW_MARGIN_S, Session, _claims_of, _collection_key

ROOT = Path(__file__).resolve().parents[2]
QUICKSTART = ROOT / "examples" / "sdk_quickstart.py"
DOC = ROOT / "docs" / "SDK.md"


# --- the quickstart is real -----------------------------------------------------------------------
def test_the_quickstart_script_exists_and_parses() -> None:
    assert QUICKSTART.exists(), "docs/SDK.md promises a runnable quickstart"
    ast.parse(QUICKSTART.read_text())


def test_the_documentation_points_at_the_script_rather_than_retyping_it() -> None:
    """The anti-drift rule.

    If the quickstart lived in the markdown as a code block, nothing would run it and it would rot silently. The
    document must reference the file.
    """
    text = DOC.read_text()
    assert "examples/sdk_quickstart.py" in text
    assert "uv run python examples/sdk_quickstart.py" in text


def test_every_method_the_quickstart_calls_exists_on_the_client() -> None:
    """Catches the quickstart drifting from the client without running a platform.

    The script is the acceptance criterion, and it needs a live stack to run. This is the part that can be
    checked in CI: a renamed or removed SDK method fails here rather than in somebody's first five minutes.
    """
    tree = ast.parse(QUICKSTART.read_text())
    called: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sio"
        ):
            called.add(node.func.attr)

    assert called, "the quickstart calls nothing on the client; it cannot be demonstrating an SDK"
    missing = sorted(name for name in called if not hasattr(SioClient, name))
    assert not missing, f"the quickstart calls methods the client does not have: {missing}"


def test_every_endpoint_the_documentation_tabulates_exists() -> None:
    """The table in the docs is a promise, and a stale one sends people to a method that is not there."""
    documented = {
        line.split("`")[1].split("(")[0]
        for line in DOC.read_text().splitlines()
        if line.startswith("| `") and "`" in line[3:]
    }
    documented -= {"method"}
    missing = sorted(name for name in documented if not hasattr(SioClient, name))
    assert not missing, f"documented but absent: {missing}"


# --- tokens ---------------------------------------------------------------------------------------
def test_a_token_is_renewed_before_it_expires() -> None:
    """A margin, so a request never begins with a token that dies mid-flight."""
    assert RENEW_MARGIN_S > 0
    fresh = Session(token="t", expires_at=9e18)
    assert fresh.valid
    expiring = Session(token="t", expires_at=__import__("time").time() + RENEW_MARGIN_S - 1)
    assert not expiring.valid, "a token inside the renewal margin must be treated as stale"
    assert not Session().valid


def test_an_explicit_token_is_trusted_indefinitely() -> None:
    """Somebody passing a token from Keycloak has their own renewal story; the SDK must not second-guess it."""
    client = SioClient(token="externally-issued")
    assert client._session.valid
    assert client._session.token == "externally-issued"


def test_the_retry_on_401_happens_once_not_in_a_loop() -> None:
    """A misconfigured secret must not become a request storm against the token endpoint.

    Asserted on the source, because the alternative is standing up a server that always 401s and counting
    requests — and the property is structural: a bounded loop.
    """
    source = inspect.getsource(SioClient.request)
    assert "for attempt in (1, 2)" in source, "the retry must be bounded to a single extra attempt"
    assert "while True" not in source


def test_claims_are_read_without_verification_and_never_crash() -> None:
    """The client reads its own freshly issued token to know when to renew.

    Not a trust decision — the server verifies, always. A client that verified its own token would be checking
    its own homework. It must therefore also never raise on a malformed one.
    """
    for token in ("", "not.a.jwt", "a.b", "x.!!!!.y", "a." + "e30" + ".c"):
        assert isinstance(_claims_of(token), dict)


# --- typed returns --------------------------------------------------------------------------------
def test_the_client_returns_models_not_dictionaries() -> None:
    """A dict-returning client pushes `row["state"]["geo"]["lat"]` into every caller.

    A renamed field then fails at the point of use with a KeyError rather than at the boundary. This platform hit
    that twice: a hand-written TypeScript type describing what its author believed, and a copilot tool reading
    `detection.frame_id`, which does not exist.
    """
    source = inspect.getsource(SioClient)
    for method in ("entities", "events", "alerts", "decisions"):
        assert f"async def {method}" in source
    assert "model_validate" in source


def test_the_entity_default_is_here_and_moving() -> None:
    """This platform deletes nothing, so "everything" is every entity that has ever existed.

    An SDK defaulting to all of history produces a first experience of confusing volume, and the caller has no
    way to know the default was the problem.
    """
    signature = inspect.signature(SioClient.entities)
    assert signature.parameters["active_within_s"].default == 300
    assert signature.parameters["include_static"].default is False


def test_the_full_record_is_still_reachable() -> None:
    """A default is a convenience, not a ceiling."""
    assert inspect.signature(SioClient.entities).parameters["active_within_s"].annotation


def test_a_wrapped_collection_is_unwrapped_without_a_lookup_table() -> None:
    """A table mapping endpoints to keys would go stale; finding the single list does not."""
    assert _collection_key({"alerts": [1, 2]}) == "alerts"
    assert _collection_key({"entities": [], "count": 3}) == "entities"
    # Ambiguous or absent falls back rather than guessing wrong.
    assert _collection_key({"a": [], "b": []}) == "items"
    assert _collection_key({"count": 3}) == "items"


# --- errors ---------------------------------------------------------------------------------------
def test_an_error_carries_the_reason_the_api_gave() -> None:
    """The API explains every denial; `HTTPError: 403` throws away the only useful part."""
    error = SioApiError(
        403,
        "decision.approve needs one of: admin, commander; you have operator",
        action="decision.approve",
        rule="decision.approve",
    )
    assert error.is_permission_error
    assert not error.is_auth_error
    assert "commander" in error.detail
    assert error.action == "decision.approve"


def test_an_auth_error_is_distinguishable_from_a_permission_error() -> None:
    """ "Your token expired" and "your role is insufficient" need different responses from a caller."""
    assert SioApiError(401, "expired").is_auth_error
    assert not SioApiError(401, "expired").is_permission_error


# --- streaming ------------------------------------------------------------------------------------
def test_the_stream_reader_handles_named_frames() -> None:
    """`EventSource.onmessage` fires only for frames with NO `event:` name.

    A reader handling only unnamed frames receives nothing from a server that names them. This platform's console
    shipped that bug and it presented as a live map that never updated.
    """
    source = inspect.getsource(SioClient._stream_once)
    assert 'startswith("event:")' in source
    assert 'startswith("data:")' in source


def test_a_malformed_frame_does_not_tear_down_the_stream() -> None:
    """A caller iterating a live feed cannot recover from an exception mid-loop."""
    source = inspect.getsource(SioClient._stream_once)
    assert "JSONDecodeError" in source
    assert "continue" in source


def test_the_stream_reconnects_with_backoff() -> None:
    source = inspect.getsource(SioClient.subscribe)
    assert "reconnect" in source
    assert "max_backoff_s" in source or "min(" in source


def test_a_stream_message_can_become_a_model() -> None:
    from sio_schemas import Event

    message = StreamMessage(
        kind="Event",
        payload={
            "event_id": "evt_1",
            "tenant_id": "t",
            "type": "zone_entered",
            "severity": "info",
            "explanation": {"summary": "test"},
        },
    )
    assert message.as_model(Event).type == "zone_entered"


# --- the copilot answer ---------------------------------------------------------------------------
def test_an_answer_carries_its_provenance() -> None:
    """An answer whose provenance you cannot inspect is one you should not act on.

    The tool calls ARE the provenance, which is why they travel with the text rather than being logged away.
    """
    answer = CopilotAnswer(
        question="what is on site?",
        text="33 entities.",
        confidence=0.7,
        trace={"tools_used": ["list_entities"]},
        redaction="Personal data was removed.",
    )
    assert answer.tools_used == ["list_entities"]
    assert answer.was_redacted
    assert str(answer) == "33 entities."


# --- the sync wrapper -----------------------------------------------------------------------------
def test_the_sync_wrapper_mirrors_the_async_client_except_for_streaming() -> None:
    """A blocking infinite iterator in a script is a trap."""
    assert not hasattr(SyncSioClient, "subscribe")
    for method in ("entities", "events", "alerts", "ask", "simulate", "analytics"):
        assert hasattr(SyncSioClient, method), f"the sync client is missing {method}"


# --- dependencies ---------------------------------------------------------------------------------
def test_the_sdk_does_not_depend_on_the_platform_internals() -> None:
    """An SDK that drags in a bus, adapters and a policy engine is not a client library.

    `pip install sio-sdk` should get somebody an HTTP client and the platform's models, not the platform.
    """
    import tomllib

    # Parsed, not string-searched. The first version of this test failed on the COMMENT in the manifest that
    # explains why `sio-core` is absent — the same false positive the plugin import test hit, where a docstring
    # mentioning a forbidden import was read as the import itself. A test that cannot tell an explanation from
    # the thing it explains will eventually be silenced rather than fixed.
    manifest = tomllib.loads((ROOT / "libs" / "sio_sdk" / "pyproject.toml").read_text())
    declared = {
        requirement.split(">")[0].split("=")[0].split("[")[0].strip()
        for requirement in manifest["project"]["dependencies"]
    }
    assert "sio-schemas" in declared
    forbidden = declared & {"sio-core", "psycopg", "fastapi", "uvicorn", "neo4j", "redis"}
    assert not forbidden, f"the SDK must not depend on the service runtime: {sorted(forbidden)}"


def test_the_sdk_imports_no_service_package() -> None:
    source = (ROOT / "libs" / "sio_sdk" / "src" / "sio_sdk" / "client.py").read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {name for name in imported if name.startswith("sio_") and name != "sio_schemas"}
    assert not forbidden, f"the SDK imports service internals: {sorted(forbidden)}"


@pytest.mark.parametrize("method", ["entities", "events", "alerts", "ask", "subscribe", "approve"])
def test_the_headline_methods_are_documented(method: str) -> None:
    """An undocumented method is one nobody finds."""
    assert f"`{method}(" in DOC.read_text() or f"`{method}`" in DOC.read_text()
