#!/usr/bin/env bash
# Start, stop and inspect SIO's infrastructure — without Docker, on either platform.
#
#   scripts/services.sh start [name...]   # default: postgres redis neo4j minio
#   scripts/services.sh stop  [name...]
#   scripts/services.sh status
#   scripts/services.sh restart
#
# Names: postgres redis neo4j minio temporal grafana ollama  (or "all")
#
# macOS delegates to `brew services`, which is what the PRD specifies and what a Mac user
# expects to see in `brew services list`. Linux launches user-owned daemons with data dirs
# under .sio/ and pidfiles under .sio/run, so nothing needs root and `just clean` is total.

set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

DEFAULT_SERVICES="postgres redis neo4j minio"
ALL_SERVICES="postgres redis neo4j minio temporal grafana ollama"

PG_DATA="${SIO_STATE_DIR}/pg"
PG_PORT="$(env_value SIO_PG_PORT 5432)"
PG_USER="$(env_value SIO_PG_USER sio)"
PG_PASSWORD="$(env_value SIO_PG_PASSWORD sio)"
PG_DATABASE="$(env_value SIO_PG_DATABASE sio)"
REDIS_PORT="6379"
NEO4J_HOME="${SIO_STATE_DIR}/neo4j"
MINIO_DATA="${SIO_STATE_DIR}/minio"

# ------------------------------------------------------------------------------ postgres
start_postgres_macos() {
  brew services start "${SIO_BREW_POSTGRES}" >/dev/null 2>&1 || true
  wait_for_port "${PG_PORT}" "postgres" || return 1
  ok "postgres running (brew services, port ${PG_PORT})"
}

start_postgres_linux() {
  bin="$(pg_bin_dir)" || die "postgres binaries not found; run scripts/bootstrap.sh"
  if [ ! -f "${PG_DATA}/PG_VERSION" ]; then
    log "initialising postgres cluster in .sio/pg"
    mkdir -p "${PG_DATA}"
    # A cluster owned by the invoking user needs no sudo and no system postgres account,
    # which keeps `just clean` honest: deleting .sio/ really does reset everything.
    "${bin}/initdb" -D "${PG_DATA}" -U "${PG_USER}" --auth=trust -E UTF8 >>"${SIO_LOG_DIR}/postgres-init.log" 2>&1
    ok "cluster initialised (owner: ${PG_USER})"
  fi
  if "${bin}/pg_ctl" -D "${PG_DATA}" status >/dev/null 2>&1; then
    ok "postgres already running (port ${PG_PORT})"
    return 0
  fi
  log "starting postgres"
  "${bin}/pg_ctl" -D "${PG_DATA}" -l "${SIO_LOG_DIR}/postgres.log" \
    -o "-p ${PG_PORT} -k ${SIO_RUN_DIR} -c listen_addresses=127.0.0.1" start >/dev/null
  wait_for_port "${PG_PORT}" "postgres" || return 1
  ok "postgres running (port ${PG_PORT}, data: .sio/pg)"
}

ensure_postgres_role_and_db() {
  bin="$(pg_bin_dir)" || return 0
  export PGPASSWORD="${PG_PASSWORD}"
  if ! "${bin}/psql" -h 127.0.0.1 -p "${PG_PORT}" -U "${PG_USER}" -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
    # macOS/brew clusters are owned by the local user, not by "sio": create the role there.
    admin_user="$(whoami)"
    "${bin}/psql" -h 127.0.0.1 -p "${PG_PORT}" -U "${admin_user}" -d postgres \
      -c "CREATE ROLE ${PG_USER} LOGIN SUPERUSER PASSWORD '${PG_PASSWORD}'" >/dev/null 2>&1 ||
      warn "could not create role ${PG_USER}; create it manually if psql fails"
  fi
  if ! "${bin}/psql" -h 127.0.0.1 -p "${PG_PORT}" -U "${PG_USER}" -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw "${PG_DATABASE}"; then
    "${bin}/createdb" -h 127.0.0.1 -p "${PG_PORT}" -U "${PG_USER}" "${PG_DATABASE}" >/dev/null 2>&1 ||
      warn "could not create database ${PG_DATABASE}"
    ok "database ${PG_DATABASE} created"
  fi
  unset PGPASSWORD
}

start_postgres() {
  if is_macos; then start_postgres_macos; else start_postgres_linux; fi
  ensure_postgres_role_and_db
}

stop_postgres() {
  if is_macos; then
    brew services stop "${SIO_BREW_POSTGRES}" >/dev/null 2>&1 || true
    ok "postgres stopped (brew services)"
  else
    bin="$(pg_bin_dir)" || return 0
    if "${bin}/pg_ctl" -D "${PG_DATA}" status >/dev/null 2>&1; then
      "${bin}/pg_ctl" -D "${PG_DATA}" -m fast stop >/dev/null 2>&1 || true
      ok "postgres stopped"
    else
      info "postgres not running"
    fi
  fi
}

# --------------------------------------------------------------------------------- redis
start_redis() {
  if is_macos; then
    brew services start "${SIO_BREW_REDIS}" >/dev/null 2>&1 || true
    wait_for_port "${REDIS_PORT}" "redis" || return 1
    ok "redis running (brew services, port ${REDIS_PORT})"
  else
    mkdir -p "${SIO_STATE_DIR}/redis"
    if port_in_use "${REDIS_PORT}"; then ok "redis already running"; return 0; fi
    start_daemon redis redis-server \
      --port "${REDIS_PORT}" --bind 127.0.0.1 --dir "${SIO_STATE_DIR}/redis" \
      --save 60 1000 --appendonly no
    wait_for_port "${REDIS_PORT}" "redis" || return 1
  fi
}

stop_redis() {
  if is_macos; then
    brew services stop "${SIO_BREW_REDIS}" >/dev/null 2>&1 || true
    ok "redis stopped (brew services)"
  else
    stop_daemon redis
  fi
}

# --------------------------------------------------------------------------------- neo4j
neo4j_admin() {
  if is_macos && have neo4j-admin; then command neo4j-admin "$@"; else "${NEO4J_HOME}/bin/neo4j-admin" "$@"; fi
}

start_neo4j() {
  if is_macos; then
    brew services start "${SIO_BREW_NEO4J}" >/dev/null 2>&1 || true
    wait_for_port 7687 "neo4j (bolt)" 80 || return 1
    ok "neo4j running (brew services, bolt 7687)"
  else
    [ -x "${NEO4J_HOME}/bin/neo4j" ] || { warn "neo4j not installed; skipping (SIO_GRAPH_BACKEND=postgres works)"; return 0; }
    if port_in_use 7687; then ok "neo4j already running"; return 0; fi
    export NEO4J_HOME
    start_daemon neo4j "${NEO4J_HOME}/bin/neo4j" console
    wait_for_port 7687 "neo4j (bolt)" 120 || return 1
  fi
}

stop_neo4j() {
  if is_macos; then
    brew services stop "${SIO_BREW_NEO4J}" >/dev/null 2>&1 || true
    ok "neo4j stopped (brew services)"
  else
    stop_daemon neo4j
  fi
}

# --------------------------------------------------------------------------------- minio
minio_bin() {
  if [ -x "${SIO_BIN_DIR}/minio" ]; then printf '%s' "${SIO_BIN_DIR}/minio"; elif have minio; then command -v minio; else return 1; fi
}

start_minio() {
  bin="$(minio_bin)" || { warn "minio not installed; skipping (SIO_BLOB_BACKEND=file works)"; return 0; }
  mkdir -p "${MINIO_DATA}"
  if port_in_use 9000; then ok "minio already running"; return 0; fi
  MINIO_ROOT_USER="$(env_value SIO_MINIO_ACCESS_KEY sioadmin)" \
  MINIO_ROOT_PASSWORD="$(env_value SIO_MINIO_SECRET_KEY sioadminsecret)" \
    start_daemon minio "${bin}" server "${MINIO_DATA}" --address 127.0.0.1:9000 --console-address 127.0.0.1:9001
  wait_for_port 9000 "minio" || return 1
}

stop_minio() { stop_daemon minio; }

# ------------------------------------------------------------------------------ temporal
temporal_bin() {
  if [ -x "${SIO_BIN_DIR}/temporal" ]; then printf '%s' "${SIO_BIN_DIR}/temporal"; elif have temporal; then command -v temporal; else return 1; fi
}

start_temporal() {
  bin="$(temporal_bin)" || { warn "temporal cli not installed; use SIO_WORKFLOW_RUNNER=inline"; return 0; }
  if port_in_use 7233; then ok "temporal already running"; return 0; fi
  start_daemon temporal "${bin}" server start-dev \
    --db-filename "${SIO_STATE_DIR}/temporal.db" --ip 127.0.0.1 --port 7233 --ui-port 8233 --headless=false
  wait_for_port 7233 "temporal" 60 || return 1
}

stop_temporal() { stop_daemon temporal; }

# ------------------------------------------------------------------------- grafana/ollama
start_grafana() {
  if is_macos; then
    brew services start "${SIO_BREW_GRAFANA}" >/dev/null 2>&1 && ok "grafana running (brew services)" ||
      warn "grafana not installed"
  else
    have grafana-server || { warn "grafana not installed; dashboards are optional"; return 0; }
    start_daemon grafana grafana-server --homepath /usr/share/grafana
  fi
}

stop_grafana() {
  if is_macos; then brew services stop "${SIO_BREW_GRAFANA}" >/dev/null 2>&1 || true; else stop_daemon grafana; fi
}

start_ollama() {
  have ollama || { warn "ollama not installed; copilot needs SIO_LLM_PROVIDER=scripted"; return 0; }
  if port_in_use 11434; then ok "ollama already running"; return 0; fi
  if is_macos; then
    brew services start "${SIO_BREW_OLLAMA}" >/dev/null 2>&1 || start_daemon ollama ollama serve
  else
    start_daemon ollama ollama serve
  fi
  wait_for_port 11434 "ollama" 40 || true
}

stop_ollama() {
  if is_macos; then brew services stop "${SIO_BREW_OLLAMA}" >/dev/null 2>&1 || true; fi
  stop_daemon ollama
}

# -------------------------------------------------------------------------------- status
status_line() {
  name="$1"; port="$2"
  if port_in_use "${port}"; then
    printf '  %s%-10s%s listening on %s\n' "${C_GREEN}" "${name}" "${C_RESET}" "${port}"
  else
    printf '  %s%-10s%s not running (port %s)\n' "${C_DIM}" "${name}" "${C_RESET}" "${port}"
  fi
}

show_status() {
  log "SIO infrastructure — $(platform_name)"
  status_line postgres "${PG_PORT}"
  status_line redis "${REDIS_PORT}"
  status_line neo4j 7687
  status_line minio 9000
  status_line temporal 7233
  status_line grafana "$(env_value SIO_GRAFANA_PORT 3000)"
  status_line ollama 11434
}

# ---------------------------------------------------------------------------------- main
action="${1:-start}"
shift || true
targets="$*"
[ -z "${targets}" ] && targets="${DEFAULT_SERVICES}"
[ "${targets}" = "all" ] && targets="${ALL_SERVICES}"

case "${action}" in
  start)
    ensure_dirs
    for svc in ${targets}; do "start_${svc}"; done
    ;;
  stop)
    [ "$*" = "" ] && targets="${ALL_SERVICES}"
    for svc in ${targets}; do "stop_${svc}"; done
    ;;
  restart)
    [ "$*" = "" ] && targets="${DEFAULT_SERVICES}"
    for svc in ${targets}; do "stop_${svc}"; done
    for svc in ${targets}; do "start_${svc}"; done
    ;;
  status) show_status ;;
  *) die "unknown action ${action} (start|stop|restart|status)" ;;
esac
