#!/usr/bin/env bash
# Verify that a SIO development environment is actually usable.
#
#   just doctor              # human-readable check of every dependency
#   just doctor --report     # same, plus a paste-able environment report for bug reports
#   just doctor --quiet      # exit status only
#
# Every check answers one question: "would the platform work right now, and if not, what is the
# single next command to run?" Anything that only *looks* correct (a binary on PATH that cannot
# connect, a database without its extensions, a bucket that exists but is not writable) is
# reported as a failure, because those are the states that waste an afternoon.

# no-errexit: the doctor's job is to check *everything* and report every problem at once.
# Exiting on the first failed probe would hide the other nine, which is the opposite of useful.
# The exit status is computed explicitly from the failure count at the end.
set -uo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

MODE="normal"
for arg in "$@"; do
  case "${arg}" in
    --report) MODE="report" ;;
    --quiet)  MODE="quiet" ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

PASS=0
FAILED=0
WARNED=0
REMEDIES=""

check_ok()   { PASS=$((PASS + 1)); [ "${MODE}" = "quiet" ] || ok "$1"; }
check_warn() { WARNED=$((WARNED + 1)); [ "${MODE}" = "quiet" ] || warn "$1"; }
check_fail() {
  FAILED=$((FAILED + 1))
  [ "${MODE}" = "quiet" ] || fail "$1"
  if [ -n "${2:-}" ]; then
    REMEDIES="${REMEDIES}
  - $2"
  fi
}

# ------------------------------------------------------------------------------ toolchain
check_tool() {
  name="$1"; remedy="$2"; required="${3:-yes}"
  if have "${name}"; then
    version="$("${name}" --version 2>&1 | head -n 1 | cut -c1-60)"
    check_ok "$(printf '%-12s %s' "${name}" "${version}")"
  elif [ "${required}" = "yes" ]; then
    check_fail "$(printf '%-12s missing' "${name}")" "${remedy}"
  else
    check_warn "$(printf '%-12s missing (optional)' "${name}")"
  fi
}

[ "${MODE}" = "quiet" ] || log "toolchain"
check_tool python3 "install python ${SIO_PYTHON_VERSION:-3.12}"
if uv_bin >/dev/null 2>&1; then
  check_ok "$(printf '%-12s %s' uv "$("$(uv_bin)" --version | cut -c1-40)")"
else
  check_fail "uv           missing" "scripts/bootstrap.sh"
fi
check_tool just "brew install just  (macOS)  |  apt-get install just  (Linux)" no
check_tool node "brew install node" no
check_tool npm "brew install node" no

# ------------------------------------------------------------------------------- python env
[ "${MODE}" = "quiet" ] || log "python environment"
if [ -d "${SIO_ROOT}/.venv" ]; then
  check_ok ".venv present"
  UV="$(uv_bin 2>/dev/null || echo uv)"
  if (cd "${SIO_ROOT}" && "${UV}" run --no-sync python -c "import sio_schemas, sio_core" >/dev/null 2>&1); then
    versions="$(cd "${SIO_ROOT}" && "${UV}" run --no-sync python -c \
      'import sio_schemas as s, sio_core as c; print(f"schemas {s.__version__} / core {c.__version__} / wire {s.SCHEMA_VERSION}")' 2>/dev/null)"
    check_ok "sio libraries importable (${versions})"
  else
    check_fail "sio libraries not importable" "just setup"
  fi
else
  check_fail ".venv missing" "just setup"
fi

# -------------------------------------------------------------------------------- datastores
PG_PORT="$(env_value SIO_PG_PORT 5432)"
[ "${MODE}" = "quiet" ] || log "datastores"

if port_in_use "${PG_PORT}"; then
  check_ok "postgres listening on ${PG_PORT}"
else
  check_fail "postgres not listening on ${PG_PORT}" "just services"
fi

if port_in_use 6379; then
  check_ok "redis listening on 6379"
else
  check_fail "redis not listening on 6379" "just services"
fi

if port_in_use 7687; then
  check_ok "neo4j listening on 7687 (bolt)"
else
  check_warn "neo4j not listening on 7687 — SIO_GRAPH_BACKEND=postgres still works"
fi

if port_in_use 9000; then
  check_ok "minio listening on 9000"
else
  check_warn "minio not listening on 9000 — SIO_BLOB_BACKEND=file still works"
fi

if port_in_use 7233; then
  check_ok "temporal listening on 7233"
else
  check_warn "temporal not running — SIO_WORKFLOW_RUNNER=inline still works"
fi

if port_in_use 11434; then
  check_ok "ollama listening on 11434"
else
  check_warn "ollama not running — copilot needs SIO_LLM_PROVIDER=scripted"
fi

# --------------------------------------------------------------- deep checks (via python)
# Ports being open proves nothing about schemas, extensions or credentials, so ask the real
# adapters. Kept in python because that is what the platform itself uses.
[ "${MODE}" = "quiet" ] || log "connectivity and initialisation"
if [ -d "${SIO_ROOT}/.venv" ]; then
  UV="$(uv_bin 2>/dev/null || echo uv)"
  probe_output="$(cd "${SIO_ROOT}" && "${UV}" run --no-sync python scripts/doctor_probe.py 2>&1)"
  probe_status=$?
  while IFS= read -r line; do
    case "${line}" in
      "ok "*)   check_ok "${line#ok }" ;;
      "warn "*) check_warn "${line#warn }" ;;
      "fail "*)
        detail="${line#fail }"
        remedy="${detail#*|}"
        [ "${remedy}" = "${detail}" ] && remedy=""
        check_fail "${detail%%|*}" "${remedy}"
        ;;
      *) [ -n "${line}" ] && info "${line}" ;;
    esac
  done <<EOF
${probe_output}
EOF
  [ "${probe_status}" -gt 1 ] && check_warn "deep probe exited with status ${probe_status}"
else
  check_warn "skipping deep checks (no .venv)"
fi

# ------------------------------------------------------------------------------------ models
[ "${MODE}" = "quiet" ] || log "models"
MODEL_DIR="${SIO_ROOT}/$(env_value SIO_MODEL_DIR .sio/models)"
for model in "$(env_value SIO_DET_MODEL yolo26n.onnx)"; do
  if [ -f "${MODEL_DIR}/${model}" ]; then
    size="$(wc -c <"${MODEL_DIR}/${model}" | tr -d ' ')"
    check_ok "${model} present (${size} bytes)"
  else
    check_warn "${model} not downloaded — run: just models"
  fi
done

# ------------------------------------------------------------------------------------ report
if [ "${MODE}" = "report" ]; then
  printf '\n%s\n' "${C_BOLD}--- paste this into a bug report ---${C_RESET}"
  echo "platform:      $(uname -srm)"
  is_macos && echo "macos:         $(sw_vers -productVersion 2>/dev/null) ($(uname -m))"
  echo "shell:         ${SHELL:-unknown} / bash ${BASH_VERSION:-?}"
  echo "python:        $(python3 --version 2>&1)"
  echo "uv:            $("$(uv_bin 2>/dev/null || echo true)" --version 2>/dev/null || echo missing)"
  echo "node:          $(node --version 2>/dev/null || echo missing)"
  echo "just:          $(just --version 2>/dev/null || echo missing)"
  is_macos && echo "brew services: $(brew services list 2>/dev/null | awk 'NR>1 {printf "%s=%s ", $1, $2}')"
  echo "state dir:     ${SIO_STATE_DIR} ($(du -sh "${SIO_STATE_DIR}" 2>/dev/null | cut -f1))"
  echo "git:           $(git -C "${SIO_ROOT}" rev-parse --short HEAD 2>/dev/null) on $(git -C "${SIO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  echo "adapters:      bus=$(env_value SIO_BUS_BACKEND redis) graph=$(env_value SIO_GRAPH_BACKEND neo4j) vector=$(env_value SIO_VECTOR_BACKEND pgvector) blob=$(env_value SIO_BLOB_BACKEND minio)"
  echo "pidfiles:      $(ls "${SIO_RUN_DIR}" 2>/dev/null | tr '\n' ' ')"
  printf '%s\n' "--- end report ---"
fi

# ----------------------------------------------------------------------------------- summary
if [ "${MODE}" != "quiet" ]; then
  printf '\n'
  if [ "${FAILED}" -eq 0 ]; then
    printf '%s\n' "${C_GREEN}${C_BOLD}healthy${C_RESET}: ${PASS} checks passed, ${WARNED} warnings"
  else
    printf '%s\n' "${C_RED}${C_BOLD}unhealthy${C_RESET}: ${FAILED} failed, ${PASS} passed, ${WARNED} warnings"
    printf '%s\n' "${C_BOLD}next steps:${C_RESET}${REMEDIES}"
  fi
fi

[ "${FAILED}" -eq 0 ] || exit 1
