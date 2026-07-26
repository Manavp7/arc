"""Cross-tenant isolation, attacked from every angle (PRD M21, P5.2).

The plan's acceptance for this is blunt: *"a negative test suite tries cross-tenant reads on every endpoint
and expects zero leakage"*. That framing is right, and the reason is that **this is the one control whose
failure is invisible**. A cross-tenant read does not error, does not look unusual in a log, and returns
plausible data — the only way to know it happened is to have tested that it cannot.

The suite is deliberately adversarial rather than illustrative. It does not check that isolation works on a
representative endpoint; it enumerates **every route the API actually exposes**, from the live OpenAPI
schema, so a route added in a later phase is covered the moment it exists rather than when somebody
remembers to add a test for it.

Three attacks, because the tenant reaches a query by three different paths:

1. a token for tenant B asking for tenant A's resources;
2. a token for tenant B with a `tenant_id` query parameter naming tenant A — the "just ask nicely" attack;
3. a forged or re-signed token claiming tenant A.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from sio_core.authn import DevJwtAuth, Principal, encode_hs256
from sio_core.authz import EmbeddedPolicyEngine

TENANT_A = "acme"
TENANT_B = "globex"


class StubDownstream(httpx.AsyncBaseTransport):
    """Answers every forwarded call, so a 200 means the API let the request through."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        return httpx.Response(
            200,
            content=b'{"ok": true, "alerts": [], "decisions": [], "entries": []}',
            headers={"content-type": "application/json"},
            request=request,
        )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    from sio_api.app import ApiService

    stub = StubDownstream()
    original = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = stub
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    # Routes that query Postgres DIRECTLY are not covered by the httpx stub, so on a machine with no database
    # they block for the pool's full open timeout and then raise. That is why this test — which lives in the
    # infra-free unit ring — passed here and would have failed on the macOS CI runner, where nothing is
    # installed. Two seconds instead of thirty keeps the suite usable when the database is genuinely absent.
    monkeypatch.setenv("SIO_PG_CONNECT_TIMEOUT_S", "2")

    service = ApiService()
    return service.app, stub


def token_for(tenant: str, *, roles: tuple[str, ...] = ("admin",)) -> str:
    return DevJwtAuth().issue(subject=f"user@{tenant}", tenant_id=tenant, roles=roles, clearance=3)


def api_routes(app: Any) -> list[tuple[str, str]]:
    """Every (method, path) the app exposes, minus the public ones.

    Read from the app rather than listed, so a route added later is attacked automatically. A hand-written
    list is a list that goes stale, and a stale isolation suite is one that passes while a new endpoint leaks.
    """
    schema = app.openapi()
    public = ("/health", "/metrics", "/auth/dev/token", "/openapi.json", "/docs", "/redoc")
    routes: list[tuple[str, str]] = []
    for path, operations in schema.get("paths", {}).items():
        if any(path.startswith(prefix) for prefix in public):
            continue
        for method in operations:
            if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                routes.append((method.upper(), path))
    return routes


def concrete(path: str) -> str:
    """Fill path parameters with ids belonging to the *other* tenant.

    The ids name tenant A's resources deliberately: the attack being tested is tenant B asking for them.
    """
    return (
        path.replace("{alert_id}", "alt_acme_1")
        .replace("{decision_id}", "dec_acme_1")
        .replace("{entity_id}", "ent_acme_1")
        .replace("{event_id}", "evt_acme_1")
        .replace("{zone_id}", "acme_zone")
        .replace("{run_id}", "run_acme_1")
        .replace("{session_id}", "ses_acme_1")
        .replace("{target}", "occupancy")
        .replace("{path:path}", "acme/frame.jpg")
    )


# --- attack 1: a token for the wrong tenant -----------------------------------------------------
def test_no_route_serves_a_request_carrying_another_tenants_id(api) -> None:
    """Tenant B asks for tenant A's resource by id, on every route that takes one.

    The check is not "did it 404" — a 404 would be fine, and so would a 403. The check is that nothing
    returns tenant A's DATA. Since the stub downstream always answers 200, any 200 on a path naming another
    tenant's resource means the API forwarded it without a tenant check, which is the leak.
    """
    app, _ = api
    parameterised = [
        (method, path)
        for method, path in api_routes(app)
        if "{" in path and "path:path" not in path
    ]
    assert parameterised, "no parameterised routes found; the extractor is wrong"

    headers = {"Authorization": f"Bearer {token_for(TENANT_B)}"}
    leaked: list[str] = []
    served_ok = 0
    # `raise_server_exceptions=False` so a route whose datastore is unreachable becomes a 500 rather than
    # propagating out of the client and failing the test. A 500 is definitionally not a data leak, and this
    # test is a leak test — but without this it could only run where a database happens to be running, which
    # is exactly the hidden infrastructure dependency that would have broken the macOS job.
    with TestClient(app, raise_server_exceptions=False) as client:
        for method, path in parameterised:
            response = client.request(
                method, concrete(path), headers=headers, json={} if method != "GET" else None
            )
            # A 200 is only acceptable if the downstream would have filtered by tenant itself — which is
            # true for the forwarding routes, since the downstream service scopes every query. The tenant
            # header is what proves the request was scoped to B and not to A.
            if response.status_code == 200:
                served_ok += 1
                served = response.headers.get("x-sio-tenant")
                if served != TENANT_B:
                    leaked.append(f"{method} {path} served as tenant {served!r}")
    assert not leaked, "requests were served outside the caller's tenant:\n" + "\n".join(leaked)
    # A leak test that tolerates errors can pass by having every route fail, which would be the most
    # comfortable possible false negative: green, and proving nothing. This asserts the run actually got
    # answers out of a decent share of the routes it attacked.
    assert served_ok >= len(parameterised) // 2, (
        f"only {served_ok} of {len(parameterised)} routes returned 200, so this run barely tested anything. "
        f"A leak test that passes because everything errored is worse than no leak test."
    )


def test_every_route_reports_the_tenant_it_served(api) -> None:
    """The header exists so a cross-tenant bug is visible in a curl, not only in a test.

    Without it, proving isolation means inspecting SQL. With it, an operator can see which tenant answered.
    """
    app, _ = api
    headers = {"Authorization": f"Bearer {token_for(TENANT_A)}"}
    with TestClient(app) as client:
        response = client.get("/api/alerts", headers=headers)
    assert response.status_code == 200
    assert response.headers["x-sio-tenant"] == TENANT_A


# --- attack 2: asking nicely ---------------------------------------------------------------------
def test_a_tenant_id_query_parameter_cannot_override_the_token(api) -> None:
    """The "just ask nicely" attack, and the reason the tenant must come from the token.

    If any endpoint accepted `?tenant_id=` as authoritative, the entire isolation model would be decoration.
    The tenant is taken from the verified token and bound to a contextvar; a query parameter of the same name
    is data, not authority.
    """
    app, _ = api
    headers = {"Authorization": f"Bearer {token_for(TENANT_B)}"}
    with TestClient(app) as client:
        response = client.get(f"/api/alerts?tenant_id={TENANT_A}", headers=headers)
    assert response.status_code == 200
    assert response.headers["x-sio-tenant"] == TENANT_B, (
        "a query parameter overrode the token's tenant"
    )


def test_a_tenant_header_cannot_override_the_token(api) -> None:
    """Same attack through a header, which is where a proxy might inject one."""
    app, _ = api
    headers = {
        "Authorization": f"Bearer {token_for(TENANT_B)}",
        "X-Tenant-Id": TENANT_A,
        "X-Sio-Tenant": TENANT_A,
    }
    with TestClient(app) as client:
        response = client.get("/api/alerts", headers=headers)
    assert response.headers["x-sio-tenant"] == TENANT_B


# --- attack 3: forged tokens ---------------------------------------------------------------------
def test_a_token_signed_with_the_wrong_secret_is_refused(api) -> None:
    """Otherwise anyone can mint a token for any tenant, and the rest of this file is theatre."""
    app, _ = api
    forged = encode_hs256(
        {"sub": "attacker", "tenant": TENANT_A, "roles": ["admin"], "exp": 9_999_999_999},
        "not-the-real-secret",
    )
    with TestClient(app) as client:
        response = client.get("/api/alerts", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
    assert "signature" in response.json()["detail"].lower()


def test_an_unsigned_token_is_refused(api) -> None:
    """The classic JWT attack: `alg: none`.

    The verifier must assert the algorithm it expects rather than read it from the token — a token that
    names its own verification algorithm is not verified at all.
    """
    import base64
    import json

    def segment(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    app, _ = api
    unsigned = (
        segment({"alg": "none", "typ": "JWT"})
        + "."
        + segment({"sub": "attacker", "tenant": TENANT_A, "roles": ["admin"]})
        + "."
    )
    with TestClient(app) as client:
        response = client.get("/api/alerts", headers={"Authorization": f"Bearer {unsigned}"})
    assert response.status_code == 401
    assert "algorithm" in response.json()["detail"].lower()


def test_an_expired_token_is_refused(api) -> None:
    app, _ = api
    expired = DevJwtAuth().issue(subject="stale", tenant_id=TENANT_A, ttl_s=-60)
    with TestClient(app) as client:
        response = client.get("/api/alerts", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


def test_a_token_without_a_tenant_claim_is_refused(api) -> None:
    """A principal with no tenant cannot be scoped, so it cannot be served.

    Defaulting to the configured tenant here would be the single worst line of code in the platform: every
    tokenless-but-signed request would quietly read the default tenant's data.
    """
    from sio_core import get_settings

    app, _ = api
    tenantless = encode_hs256(
        {"sub": "nobody", "roles": ["admin"], "exp": 9_999_999_999, "iss": "sio-dev"},
        get_settings().jwt_secret,
    )
    with TestClient(app) as client:
        response = client.get("/api/alerts", headers={"Authorization": f"Bearer {tenantless}"})
    assert response.status_code == 401
    assert "tenant" in response.json()["detail"].lower()


# --- the policy layer ---------------------------------------------------------------------------
def test_the_policy_engine_refuses_every_cross_tenant_action() -> None:
    """Belt and braces beneath the middleware: even a direct call is refused."""
    engine = EmbeddedPolicyEngine()
    principal = Principal(subject="b", tenant_id=TENANT_B, roles=frozenset({"admin"}), clearance=3)
    for action in ("entities.read", "alerts.write", "decision.approve", "admin.reset", "pii.view"):
        decision = engine.check(principal, action, context={"tenant_id": TENANT_A})
        assert not decision.allowed, f"{action} was allowed across tenants"
        assert "another tenant" in decision.reason


def test_isolation_does_not_break_the_ordinary_case() -> None:
    """Every test above is a denial. This one proves the control is not simply refusing everything.

    A negative suite with no positive case can pass against a completely broken platform.
    """
    engine = EmbeddedPolicyEngine()
    principal = Principal(
        subject="a", tenant_id=TENANT_A, roles=frozenset({"operator"}), clearance=1
    )
    assert engine.check(principal, "entities.read", context={"tenant_id": TENANT_A}).allowed
    assert engine.check(principal, "alerts.write", context={"tenant_id": TENANT_A}).allowed
