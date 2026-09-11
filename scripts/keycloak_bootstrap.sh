#!/usr/bin/env bash
# Optional local Keycloak setup. Reconcile the browser client on every run;
# retain existing containers, users, passwords and the confidential API client.
set -euo pipefail

TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REALM_FILE="${TASK_ROOT}/infra/keycloak/realm-sio.json"
PORT="${KEYCLOAK_PORT:-8080}"
VERSION="${KEYCLOAK_VERSION:-26.0}"
NAME="sio-keycloak"

runtime() {
  if command -v docker >/dev/null 2>&1; then echo docker
  elif command -v podman >/dev/null 2>&1; then echo podman
  else
    echo "  fail  neither docker nor podman is installed." >&2
    echo "        Keycloak is optional: SIO_AUTH_MODE=dev needs neither." >&2
    exit 1
  fi
}
CONTAINER="$(runtime)"
if ! command -v uv >/dev/null 2>&1; then
  echo "  fail  uv is required; run the project setup first" >&2
  exit 1
fi
uv sync --project "$TASK_ROOT" --extra keycloak
if [[ ! -f "$REALM_FILE" ]]; then
  echo "  fail  $REALM_FILE is missing" >&2
  exit 1
fi
if [[ -x "${TASK_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${TASK_ROOT}/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

if "$CONTAINER" container inspect "$NAME" >/dev/null 2>&1; then
  if [[ "$("$CONTAINER" inspect --format '{{.State.Running}}' "$NAME")" != "true" ]]; then
    "$CONTAINER" start "$NAME" >/dev/null
  fi
  echo "  ok    using existing Keycloak container; preserving realm data"
else
  echo "==> starting Keycloak $VERSION on loopback :$PORT"
  "$CONTAINER" run -d --name "$NAME" \
    -p "127.0.0.1:${PORT}:8080" \
    -e "KC_BOOTSTRAP_ADMIN_USERNAME=${KEYCLOAK_ADMIN:-admin}" \
    -e "KC_BOOTSTRAP_ADMIN_PASSWORD=${KEYCLOAK_ADMIN_PASSWORD:-admin}" \
    -v "${REALM_FILE}:/opt/keycloak/data/import/realm-sio.json:ro" \
    "quay.io/keycloak/keycloak:${VERSION}" \
    start-dev --import-realm >/dev/null
fi

# Master discovery becomes ready before optional SIO realm reconciliation.
echo -n "==> waiting for Keycloak"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/realms/master/.well-known/openid-configuration" >/dev/null 2>&1; then
    echo
    "$PYTHON" "${TASK_ROOT}/scripts/keycloak_sync.py" --url "http://127.0.0.1:${PORT}" --realm-file "$REALM_FILE"
    echo "  console: public sio-console client, authorization code + PKCE S256"
    echo "  local fixture users: operator/operator, commander/commander, zoned/zoned"
    echo "  existing user passwords and role assignments are preserved"
    echo "  then: SIO_AUTH_MODE=keycloak SIO_OIDC_DISCOVERY_URL=http://127.0.0.1:${PORT}/realms/sio/.well-known/openid-configuration SIO_KEYCLOAK_CLIENT_ID=sio-console SIO_OIDC_AUDIENCE=sio-api just dev"
    exit 0
  fi
  echo -n "."
  sleep 2
done

echo
echo "  fail  Keycloak did not become ready. Logs: $CONTAINER logs $NAME" >&2
exit 1
