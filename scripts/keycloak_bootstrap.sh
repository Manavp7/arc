#!/usr/bin/env bash
# Start Keycloak and import the SIO realm.
#
# Optional: the platform's default is a signed local JWT that is tested by the same suite. This exists so the
# claim "flipping SIO_AUTH_MODE=keycloak passes the same governance tests" can be checked rather than
# asserted.
#
# macOS is the primary target, Linux is additive — hence the branch. Both paths use the official container
# image, because Keycloak is the one dependency where running it any other way is more trouble than a
# container.
set -euo pipefail

REALM_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/infra/keycloak/realm-sio.json"
PORT="${KEYCLOAK_PORT:-8080}"
VERSION="${KEYCLOAK_VERSION:-26.0}"
NAME="sio-keycloak"

runtime() {
  if command -v docker >/dev/null 2>&1; then echo docker
  elif command -v podman >/dev/null 2>&1; then echo podman
  else
    echo "  fail  neither docker nor podman is installed." >&2
    echo "        Keycloak is optional: the default SIO_AUTH_MODE=dev needs nothing." >&2
    exit 1
  fi
}

CONTAINER="$(runtime)"

if [[ ! -f "$REALM_FILE" ]]; then
  echo "  fail  $REALM_FILE is missing" >&2
  exit 1
fi

if "$CONTAINER" ps --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "  ok    keycloak is already running on :$PORT"
  exit 0
fi

"$CONTAINER" rm -f "$NAME" >/dev/null 2>&1 || true

echo "==> starting keycloak $VERSION on :$PORT (importing realm 'sio')"
"$CONTAINER" run -d --name "$NAME" \
  -p "${PORT}:8080" \
  -e KEYCLOAK_ADMIN=admin \
  -e KEYCLOAK_ADMIN_PASSWORD=admin \
  -v "${REALM_FILE}:/opt/keycloak/data/import/realm-sio.json:ro" \
  "quay.io/keycloak/keycloak:${VERSION}" \
  start-dev --import-realm >/dev/null

# Wait for the realm, not merely the port: an open port with no realm yet produces a discovery 404, and the
# platform would report "keycloak auth is misconfigured" when it is only still starting.
echo -n "==> waiting for the realm"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/realms/sio/.well-known/openid-configuration" >/dev/null 2>&1; then
    echo
    echo "  ok    realm 'sio' is ready"
    echo
    echo "  users: operator/operator (clearance 1), commander/commander (clearance 2, pii_scope),"
    echo "         zoned/zoned (restricted to dock_1, dock_2)"
    echo
    echo "  then: SIO_AUTH_MODE=keycloak just dev"
    exit 0
  fi
  echo -n "."
  sleep 2
done

echo
echo "  fail  the realm did not become ready. Logs: $CONTAINER logs $NAME" >&2
exit 1
