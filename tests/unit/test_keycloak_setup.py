"""Browser-client import contract and idempotent local admin reconciliation.

These tests exercise fixture configuration and a fake Admin API, not a live IdP.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from sio_core.authn import principal_from_claims
from sio_core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "infra/keycloak/realm-sio.json").read_text())
SPEC = importlib.util.spec_from_file_location("keycloak_sync", ROOT / "scripts/keycloak_sync.py")
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def browser_client():
    return next(client for client in FIXTURE["clients"] if client["clientId"] == "sio-console")


def test_browser_client_requires_pkce_without_client_secret_or_password_grants():
    client = browser_client()
    assert client["publicClient"] is True
    assert "secret" not in client
    assert client["standardFlowEnabled"] is True
    assert client["implicitFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["serviceAccountsEnabled"] is False
    assert client["fullScopeAllowed"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert Settings(_env_file=None).keycloak_client_id == client["clientId"]


def test_redirects_and_logout_are_restricted_to_exact_console_urls():
    client = browser_client()
    assert set(client["redirectUris"]) == {"http://localhost:5173/", "http://127.0.0.1:5173/"}
    assert set(client["webOrigins"]) == {"http://localhost:5173", "http://127.0.0.1:5173"}
    # Keycloak '+' inherits these exact redirect URIs; there are no wildcards.
    assert client["attributes"]["post.logout.redirect.uris"] == "+"
    assert all("*" not in uri for uri in client["redirectUris"])


def test_mappers_forward_assigned_roles_and_claims_without_granting_roles():
    client = browser_client()
    mappers = {mapper["name"]: mapper for mapper in client["protocolMappers"]}
    assert all("hardcoded" not in mapper["protocolMapper"] for mapper in mappers.values())
    role_mapper = mappers["sio-realm-roles"]
    assert role_mapper["protocolMapper"] == "oidc-usermodel-realm-role-mapper"
    assert role_mapper["config"]["claim.name"] == "realm_access.roles"
    assert (
        mappers["sio-api-audience"]["config"]["included.client.audience"]
        == Settings(_env_file=None).oidc_audience
    )
    for claim in ("tenant", "clearance", "pii_scope", "zones"):
        assert mappers[claim]["config"]["user.attribute"] == claim
        assert mappers[claim]["config"]["claim.name"] == claim
    allowed = set(
        next(row["roles"] for row in FIXTURE["scopeMappings"] if row["client"] == "sio-console")
    )
    assert allowed == {"viewer", "operator", "commander", "integrator", "ml_engineer", "admin"}
    for user in FIXTURE["users"]:
        attrs = user["attributes"]
        claims = {
            "sub": user["username"],
            "tenant": attrs["tenant"][0],
            "clearance": int(attrs["clearance"][0]),
            "realm_access": {"roles": list(set(user["realmRoles"]) & allowed)},
        }
        principal = principal_from_claims(claims)
        assert principal.roles <= set(user["realmRoles"])
        assert "admin" not in principal.roles
    assert "service" not in allowed


class FakeAdmin:
    def __init__(self, fixture=None):
        self.fixture = copy.deepcopy(fixture)
        self.clients = {}
        self.roles = {}
        self.scopes = {}
        self.writes = []
        if fixture:
            self.import_fixture(fixture)

    def import_fixture(self, fixture):
        self.fixture = copy.deepcopy(fixture)
        self.roles = {
            role["name"]: {**role, "id": f"role-{role['name']}"}
            for role in fixture["roles"]["realm"]
        }
        for client in fixture["clients"]:
            self.add_client(client)
        for mapping in fixture.get("scopeMappings", []):
            self.scopes[mapping["client"]] = [self.roles[name] for name in mapping["roles"]]

    def add_client(self, body):
        client = copy.deepcopy(body)
        client["id"] = f"id-{client['clientId']}"
        client["protocolMappers"] = [
            {**mapper, "id": f"mapper-{mapper['name']}"}
            for mapper in client.get("protocolMappers", [])
        ]
        self.clients[client["clientId"]] = client

    def request(self, method, path, body=None):
        body = copy.deepcopy(body)
        if method != "GET":
            self.writes.append((method, path, body))
        if path == "/admin/realms":
            self.import_fixture(body)
            return None
        if path == "/admin/realms/sio":
            if self.fixture is None:
                raise sync.AdminError(404, method, path)
            return copy.deepcopy(self.fixture)
        parsed = urlsplit(path)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if parts[3] == "roles":
            if method == "POST":
                self.roles[body["name"]] = {**body, "id": f"role-{body['name']}"}
                return None
            if parts[4] not in self.roles:
                raise sync.AdminError(404, method, path)
            return copy.deepcopy(self.roles[parts[4]])
        if len(parts) == 4:
            if method == "POST":
                self.add_client(body)
                return None
            name = parse_qs(parsed.query)["clientId"][0]
            return [copy.deepcopy(self.clients[name])] if name in self.clients else []
        client = next(client for client in self.clients.values() if client["id"] == parts[4])
        if len(parts) == 5:
            if method == "PUT":
                self.clients[client["clientId"]] = body
                return None
            return copy.deepcopy(client)
        if parts[5] == "protocol-mappers":
            if method == "GET":
                return copy.deepcopy(client["protocolMappers"])
            if method == "POST":
                client["protocolMappers"].append({**body, "id": f"mapper-{body['name']}"})
            else:
                client["protocolMappers"] = [
                    m for m in client["protocolMappers"] if m["id"] != parts[7]
                ]
                if method == "PUT":
                    client["protocolMappers"].append(body)
            return None
        roles = self.scopes.setdefault(client["clientId"], [])
        if method == "GET":
            return copy.deepcopy(roles)
        if method == "DELETE":
            self.scopes[client["clientId"]] = [
                role for role in roles if role["name"] not in {r["name"] for r in body}
            ]
        else:
            roles.extend(body)
        return None


def test_bootstrap_new_realm_is_idempotent():
    api = FakeAdmin()
    sync.reconcile(api, FIXTURE)
    assert api.clients["sio-console"]["publicClient"] is True
    api.writes.clear()
    sync.reconcile(api, FIXTURE)
    assert api.writes == []


def test_existing_realm_keeps_users_passwords_and_other_clients():
    existing = copy.deepcopy(FIXTURE)
    existing["clients"] = [
        client for client in existing["clients"] if client["clientId"] != "sio-console"
    ]
    existing["scopeMappings"] = []
    existing["users"][0]["credentials"][0]["value"] = "changed-locally"
    existing["clients"][0]["secret"] = "private-api-secret"
    api = FakeAdmin(existing)
    sync.reconcile(api, FIXTURE)
    assert api.fixture["users"] == existing["users"]
    assert api.clients["sio-api"]["secret"] == "private-api-secret"
    assert not any("users" in path for _, path, _ in api.writes)
    api.writes.clear()
    sync.reconcile(api, FIXTURE)
    assert api.writes == []


def test_managed_client_reconciliation_removes_unsafe_mapper_and_service_scope():
    api = FakeAdmin(FIXTURE)
    api.clients["sio-console"]["protocolMappers"].append(
        {"id": "unsafe", "name": "grant-admin", "protocolMapper": "oidc-hardcoded-role-mapper"}
    )
    api.scopes["sio-console"].append(api.roles["service"])
    sync.reconcile(api, FIXTURE)
    assert "service" not in {role["name"] for role in api.scopes["sio-console"]}
    assert all(mapper["id"] != "unsafe" for mapper in api.clients["sio-console"]["protocolMappers"])
    assert api.fixture["users"] == FIXTURE["users"]
