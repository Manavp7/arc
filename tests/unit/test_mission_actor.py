"""Mission testimony must name the authenticated actor, regardless of client-supplied names."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sio_missions.service import MissionsService

from sio_core.authn import DevJwtAuth


@pytest.fixture
def missions(settings, memory_bus):
    settings.audit_enabled = False
    service = MissionsService(settings, bus=memory_bus)
    service.pool = SimpleNamespace(
        execute=AsyncMock(return_value=1), fetchrow=AsyncMock(return_value=None)
    )
    service._load = AsyncMock(
        return_value={
            "mission_id": "m1",
            "name": "Response",
            "state": "active",
            "payload": {
                "objectives": [{"objective_id": "o1", "description": "Confirm area is safe"}]
            },
        }
    )
    service._render = AsyncMock(return_value={"mission_id": "m1"})
    service._comms = AsyncMock(return_value=[])
    return service


def headers(service, *, cookie=False, role="commander"):
    token = DevJwtAuth(service.settings).issue(
        subject="console", tenant_id=service.settings.tenant_id, roles=(role,), clearance=3
    )
    return {"cookie": f"sio_token={token}"} if cookie else {"Authorization": f"Bearer {token}"}


def writes(service, table):
    return [
        call.args[1]
        for call in service.pool.execute.await_args_list
        if f"INSERT INTO {table} " in call.args[0]
    ]


@pytest.mark.parametrize("cookie", [False, True])
@pytest.mark.parametrize("commander", [None, "assigned-commander"])
def test_creation_records_actor_separately_from_commander(missions, cookie, commander):
    with TestClient(missions.app) as client:
        response = client.post(
            "/missions",
            json={"name": "Response", "commander": commander},
            headers=headers(missions, cookie=cookie, role="operator"),
        )
    assert response.status_code == 200
    assert writes(missions, "missions")[0][4] == commander
    assert writes(missions, "mission_comms")[0][3] == "console"


def test_commander_override_records_verified_actor_and_preserves_automatic_authorship(missions):
    with TestClient(missions.app) as client:
        response = client.post(
            "/missions/m1/state?to=completed&force=true&by=impersonated-commander",
            headers=headers(missions),
        )
    assert response.status_code == 200
    comms = writes(missions, "mission_comms")
    assert [comm[3] for comm in comms] == ["console", "console", "platform"]
    assert "by decision of console" in comms[1][5]
    assert "impersonated" not in str(comms)


def test_resource_assignment_persists_verified_actor(missions):
    with TestClient(missions.app) as client:
        response = client.post(
            "/missions/m1/resources?resource_id=r1&by=impersonated-commander",
            headers=headers(missions),
        )
    assert response.status_code == 200
    assert writes(missions, "mission_resources")[0][3] == "console"
    assert writes(missions, "mission_comms")[0][3] == "console"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("DELETE", "/missions/m1/resources/r1?by=impersonated-commander", None),
        ("POST", "/missions/m1/objectives", {"description": "Inspect dock"}),
        ("POST", "/missions/m1/objectives/o1?by=impersonated-commander", None),
        (
            "POST",
            "/missions/m1/comms",
            {"body": "Area checked", "author": "impersonated-commander"},
        ),
    ],
)
def test_manual_mission_actions_use_verified_actor(missions, method, path, body):
    with TestClient(missions.app) as client:
        response = client.request(method, path, json=body, headers=headers(missions))
    assert response.status_code == 200
    assert writes(missions, "mission_comms")[0][3] == "console"
    if "/objectives/o1" in path:
        stored = json.loads(missions.pool.execute.await_args_list[0].args[1][0])
        assert stored["objectives"][0]["satisfied_by"] == ["console"]


def test_unprivileged_actor_cannot_self_declare_commander_approval(missions):
    with TestClient(missions.app) as client:
        response = client.post(
            "/missions/m1/state?to=completed&force=true&by=commander",
            headers=headers(missions, role="operator"),
        )
    assert response.status_code == 403
    missions.pool.execute.assert_not_awaited()
