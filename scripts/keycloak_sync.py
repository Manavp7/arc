"""Idempotently provision the managed browser client in the local SIO realm.

A new realm imports the development fixture. An existing realm keeps its users,
passwords, realm settings and other clients. Only sio-console and its own mapper
and role-scope configuration are reconciled. No role is assigned to a user.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

CLIENT_ID = "sio-console"


class AdminError(RuntimeError):
    def __init__(self, status: int, method: str, path: str) -> None:
        self.status = status
        super().__init__(f"Keycloak admin request failed: {method} {path} (HTTP {status})")


class AdminAPI:
    def __init__(self, url: str, username: str, password: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("This development helper only manages Keycloak on HTTP loopback")
        self.url = url.rstrip("/")
        self.token = ""
        body = urlencode(
            {
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": username,
                "password": password,
            }
        ).encode()
        request = Request(
            f"{self.url}/realms/master/protocol/openid-connect/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                self.token = json.load(response)["access_token"]
        except HTTPError as exc:
            raise AdminError(exc.code, "POST", "admin authentication") from None

    def request(self, method: str, path: str, body: Any = None) -> Any:
        request = Request(
            f"{self.url}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=15) as response:
                data = response.read()
                return json.loads(data) if data else None
        except HTTPError as exc:
            # Error bodies may contain request data; never print credentials.
            raise AdminError(exc.code, method, path) from None


def reconcile(api: Any, fixture: dict[str, Any]) -> dict[str, Any]:
    realm = fixture["realm"]
    root = f"/admin/realms/{quote(realm, safe='')}"
    try:
        api.request("GET", root)
    except AdminError as exc:
        if exc.status != 404:
            raise
        api.request("POST", "/admin/realms", fixture)

    desired = next(client for client in fixture["clients"] if client["clientId"] == CLIENT_ID)
    managed_roles = next(
        mapping["roles"]
        for mapping in fixture["scopeMappings"]
        if mapping.get("client") == CLIENT_ID
    )
    clients_path = f"{root}/clients"
    lookup = f"{clients_path}?{urlencode({'clientId': CLIENT_ID})}"
    clients = [client for client in api.request("GET", lookup) if client["clientId"] == CLIENT_ID]
    if len(clients) > 1:
        raise ValueError("Multiple sio-console clients found; refusing an ambiguous update")
    client_configuration = {
        key: value for key, value in desired.items() if key != "protocolMappers"
    }
    if not clients:
        api.request("POST", clients_path, client_configuration)
        clients = [
            client for client in api.request("GET", lookup) if client["clientId"] == CLIENT_ID
        ]
    if len(clients) != 1:
        raise ValueError("The managed browser client could not be located after creation")
    client_path = f"{clients_path}/{quote(clients[0]['id'], safe='')}"
    existing = api.request("GET", client_path)
    if any(existing.get(key) != value for key, value in client_configuration.items()):
        api.request("PUT", client_path, {**existing, **client_configuration})

    mapper_path = f"{client_path}/protocol-mappers/models"
    existing_mappers = {mapper["name"]: mapper for mapper in api.request("GET", mapper_path)}
    desired_names = {mapper["name"] for mapper in desired["protocolMappers"]}
    # This client is package-managed. Retaining an extra hardcoded-role mapper
    # could elevate every person who signs in, so reconcile its mappers exactly.
    for name, mapper in existing_mappers.items():
        if name not in desired_names:
            api.request("DELETE", f"{mapper_path}/{quote(mapper['id'], safe='')}")
    for mapper in desired["protocolMappers"]:
        existing_mapper = existing_mappers.get(mapper["name"])
        if existing_mapper is None:
            api.request("POST", mapper_path, mapper)
        elif any(existing_mapper.get(key) != value for key, value in mapper.items()):
            api.request(
                "PUT",
                f"{mapper_path}/{quote(existing_mapper['id'], safe='')}",
                {**mapper, "id": existing_mapper["id"]},
            )

    scope_path = f"{client_path}/scope-mappings/realm"
    current_roles = api.request("GET", scope_path)
    extra_roles = [role for role in current_roles if role["name"] not in managed_roles]
    if extra_roles:
        api.request("DELETE", scope_path, extra_roles)
    current_names = {role["name"] for role in current_roles}
    required = []
    for name in managed_roles:
        if name in current_names:
            continue
        role_path = f"{root}/roles/{quote(name, safe='')}"
        try:
            role = api.request("GET", role_path)
        except AdminError as exc:
            if exc.status != 404:
                raise
            definition = next(role for role in fixture["roles"]["realm"] if role["name"] == name)
            api.request("POST", f"{root}/roles", definition)
            role = api.request("GET", role_path)
        required.append(role)
    if required:
        api.request("POST", scope_path, required)
    return {"realm": realm, "client": CLIENT_ID, "roles": list(managed_roles)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--realm-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "infra/keycloak/realm-sio.json",
    )
    args = parser.parse_args()
    # SIO-ENV-OK: these are credentials for this optional local bootstrap CLI.
    api = AdminAPI(
        args.url,
        os.environ.get("KEYCLOAK_ADMIN", "admin"),
        os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "admin"),
    )
    result = reconcile(api, json.loads(args.realm_file.read_text()))
    print(
        f"  ok    realm {result['realm']!r}, public client {result['client']!r} reconciled; user roles unchanged"
    )


if __name__ == "__main__":
    main()
