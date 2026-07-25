#!/usr/bin/env bash
# Start OPA with the generated SIO policy.
#
# The policy is GENERATED from `sio_core.authz.POLICY` by `just policies`, and a test asserts the checked-in
# file matches. Two hand-written implementations of one authorisation policy drift, and the drift is a
# permission difference between environments.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POLICY_DIR="$ROOT/infra/opa/policies"
PORT="${OPA_PORT:-8181}"

install_hint_macos() {
  # Named `*_macos` so the portability lint can see that this only ever runs on macOS. That lint fired on the
  # first version of this file, where the advice sat inline: it is checking the enclosing scope rather than
  # guessing at intent, and printing OS-specific advice from an OS-specific function is both what it wants
  # and better UX than showing a reader two commands and letting them pick.
  echo "        brew install opa" >&2
}

install_hint_linux() {
  echo "        curl -L -o /usr/local/bin/opa \\" >&2
  echo "          https://openpolicyagent.org/downloads/latest/opa_linux_amd64_static" >&2
  echo "        chmod +x /usr/local/bin/opa" >&2
}

if ! command -v opa >/dev/null 2>&1; then
  echo "  fail  the opa binary is not installed. Install it with:" >&2
  if [[ "$(uname -s)" == "Darwin" ]]; then
    install_hint_macos
  else
    install_hint_linux
  fi
  echo "        OPA is optional: the default SIO_POLICY_ENGINE=embedded evaluates the same rules." >&2
  exit 1
fi

if [[ ! -f "$POLICY_DIR/sio.rego" ]]; then
  echo "  fail  $POLICY_DIR/sio.rego is missing. Run: just policies" >&2
  exit 1
fi

# Compile before serving. A policy that fails to compile would otherwise leave OPA answering `false` to
# everything — and since OpaPolicyEngine denies on an unreachable or unhelpful OPA, that presents as the
# entire platform refusing every request.
echo "==> checking the policy compiles"
opa check "$POLICY_DIR/sio.rego"

echo "==> starting opa on :$PORT"
exec opa run --server --addr "127.0.0.1:${PORT}" --log-level error "$POLICY_DIR"
