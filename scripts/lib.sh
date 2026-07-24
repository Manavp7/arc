#!/usr/bin/env bash
# Shared shell helpers for SIO scripts.
#
# Portability rules enforced by tests/unit/test_scripts_portability.py:
#   * bash 3.2 compatible (that is what ships on macOS) — no associative arrays, no `mapfile`;
#   * no GNU-only flags: `sed -i` without a suffix, `readlink -f`, `date -d`, `grep -P` are out;
#   * every platform-specific command sits behind is_macos/is_linux.
# macOS is the supported product; Linux is additive. Neither branch may break the other.

set -euo pipefail

SIO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIO_STATE_DIR="${SIO_DATA_DIR:-${SIO_ROOT}/.sio}"
SIO_BIN_DIR="${SIO_STATE_DIR}/bin"
SIO_LOG_DIR="${SIO_STATE_DIR}/logs"
SIO_RUN_DIR="${SIO_STATE_DIR}/run"

# shellcheck disable=SC1091
[ -f "${SIO_ROOT}/scripts/versions.env" ] && . "${SIO_ROOT}/scripts/versions.env"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_DIM=""; C_BOLD=""
fi

log()   { printf '%s\n' "${C_BLUE}==>${C_RESET} $*"; }
ok()    { printf '%s\n' "  ${C_GREEN}ok${C_RESET}    $*"; }
warn()  { printf '%s\n' "  ${C_YELLOW}warn${C_RESET}  $*" >&2; }
fail()  { printf '%s\n' "  ${C_RED}fail${C_RESET}  $*" >&2; }
info()  { printf '%s\n' "  ${C_DIM}$*${C_RESET}"; }
die()   { fail "$*"; exit 1; }

is_macos() { [ "$(uname -s)" = "Darwin" ]; }
is_linux() { [ "$(uname -s)" = "Linux" ]; }

platform_name() {
  if is_macos; then echo "macOS"; elif is_linux; then echo "Linux"; else uname -s; fi
}

have() { command -v "$1" >/dev/null 2>&1; }

ensure_dirs() {
  mkdir -p "${SIO_STATE_DIR}" "${SIO_BIN_DIR}" "${SIO_LOG_DIR}" "${SIO_RUN_DIR}"
}

# Read a value from .env (falling back to .env.example), without sourcing the file:
# values may contain characters that a shell would interpret.
env_value() {
  key="$1"; default_value="${2:-}"
  for file in "${SIO_ROOT}/.env" "${SIO_ROOT}/.env.example"; do
    [ -f "${file}" ] || continue
    value="$(grep "^${key}=" "${file}" 2>/dev/null | head -n 1 | cut -d= -f2- || true)"
    if [ -n "${value}" ]; then
      printf '%s' "${value}"
      return 0
    fi
  done
  printf '%s' "${default_value}"
}

port_in_use() {
  port="$1"
  if have lsof; then
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
  elif have nc; then
    nc -z 127.0.0.1 "${port}" >/dev/null 2>&1
  else
    return 1
  fi
}

wait_for_port() {
  port="$1"; label="${2:-port ${1}}"; attempts="${3:-40}"
  i=0
  while [ "${i}" -lt "${attempts}" ]; do
    if port_in_use "${port}"; then
      return 0
    fi
    i=$((i + 1))
    sleep 0.5
  done
  fail "${label} did not come up on port ${port} (waited $((attempts / 2))s)"
  return 1
}

pidfile_for() { printf '%s/%s.pid' "${SIO_RUN_DIR}" "$1"; }

# Start a background daemon owned by this repo, recording its pid so `just stop` is precise
# and never resorts to killing processes by name.
start_daemon() {
  name="$1"; shift
  ensure_dirs
  pidfile="$(pidfile_for "${name}")"
  if daemon_running "${name}"; then
    ok "${name} already running (pid $(cat "${pidfile}"))"
    return 0
  fi
  log "starting ${name}"
  "$@" >>"${SIO_LOG_DIR}/${name}.log" 2>&1 &
  echo $! >"${pidfile}"
  ok "${name} started (pid $(cat "${pidfile}")), log: .sio/logs/${name}.log"
}

daemon_running() {
  pidfile="$(pidfile_for "$1")"
  [ -f "${pidfile}" ] || return 1
  pid="$(cat "${pidfile}" 2>/dev/null || true)"
  [ -n "${pid}" ] || return 1
  kill -0 "${pid}" 2>/dev/null
}

stop_daemon() {
  name="$1"
  pidfile="$(pidfile_for "${name}")"
  if [ ! -f "${pidfile}" ]; then
    info "${name} not running"
    return 0
  fi
  pid="$(cat "${pidfile}" 2>/dev/null || true)"
  if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
    i=0
    while [ "${i}" -lt 20 ] && kill -0 "${pid}" 2>/dev/null; do
      i=$((i + 1)); sleep 0.25
    done
    if kill -0 "${pid}" 2>/dev/null; then
      warn "${name} (pid ${pid}) ignored SIGTERM; sending SIGKILL"
      kill -9 "${pid}" 2>/dev/null || true
    fi
    ok "${name} stopped"
  else
    info "${name} not running (stale pidfile)"
  fi
  rm -f "${pidfile}"
}

download() {
  url="$1"; dest="$2"
  if have curl; then
    curl -fsSL --retry 3 --retry-delay 2 -o "${dest}" "${url}"
  elif have wget; then
    wget -q -O "${dest}" "${url}"
  else
    die "neither curl nor wget is available"
  fi
}

# Locate the Postgres binaries. Homebrew keeps versioned formulae out of PATH, and Debian
# hides them under /usr/lib/postgresql/<version>/bin, so neither platform can rely on PATH.
pg_bin_dir() {
  if have pg_ctl && have initdb; then
    dirname "$(command -v pg_ctl)"
    return 0
  fi
  for candidate in \
    "/opt/homebrew/opt/${SIO_BREW_POSTGRES:-postgresql@16}/bin" \
    "/usr/local/opt/${SIO_BREW_POSTGRES:-postgresql@16}/bin" \
    "/usr/lib/postgresql/16/bin" \
    "/usr/lib/postgresql/17/bin"; do
    if [ -x "${candidate}/pg_ctl" ]; then
      printf '%s' "${candidate}"
      return 0
    fi
  done
  return 1
}

uv_bin() {
  if have uv; then command -v uv; return 0; fi
  for candidate in "${HOME}/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
    [ -x "${candidate}" ] && { printf '%s' "${candidate}"; return 0; }
  done
  return 1
}
