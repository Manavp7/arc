"""Every route on every service resolves to a governed action (PRD M18, Phase 5).

This file exists because the design worked and I still got it wrong. `GET /audit` on the governance service's
own port resolved to `unmapped.request`, was denied by default, and said so in the log — the intended
behaviour for an unrecognised route, and a completely broken endpoint.

That is the trade the deny-by-default fallback makes: a route nobody mapped is **visibly** ungoverned rather
than **invisibly** unprotected. It is the right trade, and it means the mapping needs a test, because the
failure mode is now "the feature is broken" rather than "the feature is insecure" — noisier, but still a
failure.

So this walks every service's real FastAPI app, enumerates every route, and asserts each one resolves to an
action the policy defines. A service added in a later phase fails here the moment it has routes, rather than
when somebody tries to use them.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from sio_core.authz import POLICY
from sio_core.guard import action_for, is_public

#: Every service with an HTTP surface, and the module that builds it.
#:
#: Listed rather than discovered, because a discovery mechanism that silently found nothing would make this
#: whole file pass vacuously — and a test that cannot fail is worse than no test.
SERVICES: tuple[tuple[str, str], ...] = (
    ("api", "sio_api.app:ApiService"),
    ("alerts", "sio_alerts.service:AlertsService"),
    ("governance", "sio_governance.service:GovernanceService"),
    ("decision", "sio_decision.service:DecisionService"),
    ("workflow", "sio_workflow.service:WorkflowService"),
    ("agents", "sio_agents.service:AgentsService"),
    ("copilot", "sio_copilot.service:CopilotService"),
    ("prediction", "sio_prediction.service:PredictionService"),
    ("spatial", "sio_spatial.service:SpatialService"),
    ("events", "sio_events.service:EventsService"),
    ("worldmodel", "sio_worldmodel.service:WorldModelService"),
    ("perception", "sio_perception.service:PerceptionService"),
    ("tracking", "sio_tracking.service:TrackingService"),
    ("fusion", "sio_fusion.service:FusionService"),
    ("ingest", "sio_ingest.service:IngestService"),
    ("simulation", "sio_simulation.service:SimulationService"),
    ("analytics", "sio_analytics.service:AnalyticsService"),
    ("webhooks", "sio_webhooks.service:WebhooksService"),
    ("missions", "sio_missions.service:MissionsService"),
)


def build(spec: str) -> Any:
    module_name, class_name = spec.split(":")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


def routes_of(app: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path, operations in app.openapi().get("paths", {}).items():
        for method in operations:
            if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                found.append((method.upper(), path))
    return found


def action_is_defined(action: str) -> bool:
    return any(rule.matches(action) for rule in POLICY)


@pytest.mark.parametrize(("name", "spec"), SERVICES, ids=[name for name, _ in SERVICES])
def test_every_route_resolves_to_a_defined_action(name: str, spec: str) -> None:
    """A route mapping to `unmapped.request` is denied to everyone, including admins.

    Which means it is not a security hole — it is a broken feature, and it will present as "this endpoint
    returns 403 for everybody" rather than as a leak. Still worth catching before a user does.
    """
    service = build(spec)
    unmapped: list[str] = []
    for method, path in routes_of(service.app):
        if is_public(path):
            continue
        action = action_for(method, path)
        if action == "unmapped.request" or not action_is_defined(action):
            unmapped.append(f"{method} {path} -> {action}")
    assert not unmapped, (
        f"{name} has routes that no policy rule governs, so they are denied to everyone:\n"
        + "\n".join(unmapped)
        + "\n\nAdd a prefix to sio_core.guard.RESOURCE_PREFIXES or a rule to sio_core.authz.POLICY."
    )


def test_the_service_list_is_not_empty_or_stale() -> None:
    """Guards against the guard: a discovery mechanism that found nothing would pass vacuously."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "services"
    on_disk = {path.name for path in root.iterdir() if path.is_dir()}
    listed = {name for name, _ in SERVICES}
    # `mcp` speaks MCP rather than plain HTTP and is covered by its own tests.
    missing = on_disk - listed - {"mcp"}
    assert not missing, f"these services are not covered by this test: {sorted(missing)}"


@pytest.mark.parametrize(
    "path",
    ["/health", "/metrics", "/auth/dev/token", "/openapi.json", "/docs", "/redoc"],
)
def test_the_public_paths_are_exactly_the_ones_that_must_be(path: str) -> None:
    """Two kinds only, each for a stated reason.

    Liveness, because a supervisor cannot hold a token before the token issuer is up. The token endpoint,
    because nothing can present a token before obtaining one. Every further exclusion is a hole that will
    eventually be found, so there are none.
    """
    assert is_public(path)


def test_nothing_else_is_public() -> None:
    for path in ("/api/entities", "/api/alerts", "/audit", "/policies", "/copilot/ask", "/"):
        assert not is_public(path), f"{path} is served without a principal"


def test_the_most_consequential_actions_need_more_than_a_role() -> None:
    """Approving an action and reading personal data both need a second factor.

    Stated as a test because it is the property most likely to be relaxed by someone in a hurry: a role check
    alone is easy to add and easy to over-grant, and both of these authorise something irreversible — a
    dispatch into the physical world, or a disclosure that cannot be taken back.
    """
    by_action = {rule.action: rule for rule in POLICY}
    approve = by_action["decision.approve"]
    assert approve.roles and approve.min_clearance >= 2

    pii = by_action["pii.view"]
    assert pii.roles and pii.requires_pii_scope and pii.min_clearance >= 2

    raw = by_action["media.raw"]
    assert raw.roles and raw.requires_pii_scope
