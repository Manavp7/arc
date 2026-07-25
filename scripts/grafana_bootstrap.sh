#!/usr/bin/env bash
# Start Grafana with the provisioned SIO datasources and dashboards.
#
# Optional, and Grafana is the one dependency in this stack that is genuinely easier as a container: it wants a
# writable data directory, a plugin path and a config file, and reproducing that on two operating systems is
# more work than it is worth for a dashboard viewer.
#
# The dashboards are provisioned from files with `allowUiUpdates: false`, so a change is a reviewable diff
# rather than something somebody clicked together — and a test asserts every panel references a metric the
# platform actually exports, because a dashboard of empty panels renders without error and reads as a dead
# pipeline.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${GRAFANA_PORT:-3000}"
VERSION="${GRAFANA_VERSION:-11.4.0}"
NAME="sio-grafana"

runtime() {
  if command -v docker >/dev/null 2>&1; then echo docker
  elif command -v podman >/dev/null 2>&1; then echo podman
  else
    echo "  fail  neither docker nor podman is installed." >&2
    echo "        Grafana is optional: /metrics is scrapeable without it, and the in-app" >&2
    echo "        analytics at /analytics/summary need nothing extra." >&2
    exit 1
  fi
}

CONTAINER="$(runtime)"

if "$CONTAINER" ps --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "  ok    grafana is already running on :$PORT"
  exit 0
fi

"$CONTAINER" rm -f "$NAME" >/dev/null 2>&1 || true

echo "==> starting grafana $VERSION on :$PORT"
"$CONTAINER" run -d --name "$NAME" \
  -p "${PORT}:3000" \
  -e GF_SECURITY_ADMIN_USER=admin \
  -e GF_SECURITY_ADMIN_PASSWORD=admin \
  -e GF_USERS_ALLOW_SIGN_UP=false \
  -e GF_AUTH_ANONYMOUS_ENABLED=true \
  -e GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
  --add-host host.docker.internal:host-gateway \
  -v "${ROOT}/infra/grafana/provisioning:/etc/grafana/provisioning:ro" \
  -v "${ROOT}/infra/grafana/dashboards:/var/lib/grafana/dashboards:ro" \
  "grafana/grafana:${VERSION}" >/dev/null

# Wait for the DASHBOARD, not merely the port. An open port with no dashboard yet means provisioning has not
# run, and a reader who opens Grafana at that moment sees an empty folder and concludes it did not work.
echo -n "==> waiting for provisioning"
for _ in $(seq 1 45); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/dashboards/uid/sio-pipeline" >/dev/null 2>&1; then
    echo
    echo "  ok    dashboard 'SIO — pipeline' provisioned"
    echo
    echo "  open: http://localhost:${PORT}/d/sio-pipeline (anonymous viewer is enabled)"
    echo
    echo "  NOTE: the Prometheus datasource expects a Prometheus scraping the services' /metrics"
    echo "        endpoints on :9090. Without one, the Postgres-backed panels still work and the"
    echo "        Prometheus panels are empty — which is a missing scraper, not a broken dashboard."
    exit 0
  fi
  echo -n "."
  sleep 2
done

echo
echo "  fail  provisioning did not complete. Logs: $CONTAINER logs $NAME" >&2
exit 1
