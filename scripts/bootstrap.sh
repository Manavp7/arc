#!/usr/bin/env bash
# Install everything SIO needs, idempotently.
#
#   scripts/bootstrap.sh            # default: datastores + toolchain + python/node deps
#   scripts/bootstrap.sh --minimal  # postgres + redis + deps only (no neo4j/minio/temporal/ollama)
#   scripts/bootstrap.sh --full     # everything, including grafana, ollama and gstreamer
#   scripts/bootstrap.sh --deps     # only python/node dependencies
#
# macOS (the supported platform per the PRD) uses Homebrew, no Docker.
# Linux is additive, for CI and verification: apt for postgres/redis, user-owned tarballs for
# the rest, so that state stays under .sio/ and `just clean` is complete.

set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

PROFILE="default"
for arg in "$@"; do
  case "${arg}" in
    --minimal) PROFILE="minimal" ;;
    --full)    PROFILE="full" ;;
    --deps)    PROFILE="deps" ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown argument: ${arg} (try --help)" ;;
  esac
done

log "SIO bootstrap — platform: $(platform_name), profile: ${PROFILE}"
ensure_dirs

# --------------------------------------------------------------------------- env file
if [ ! -f "${SIO_ROOT}/.env" ]; then
  cp "${SIO_ROOT}/.env.example" "${SIO_ROOT}/.env"
  ok "created .env from .env.example"
else
  ok ".env already present (left untouched)"
fi

# ------------------------------------------------------------------------- toolchain
install_uv() {
  if uv_bin >/dev/null 2>&1; then
    ok "uv $("$(uv_bin)" --version | awk '{print $2}')"
    return 0
  fi
  log "installing uv"
  if is_macos && have brew; then
    brew install uv
  else
    download "https://astral.sh/uv/install.sh" "${SIO_STATE_DIR}/uv-install.sh"
    sh "${SIO_STATE_DIR}/uv-install.sh" >/dev/null
  fi
  uv_bin >/dev/null 2>&1 || die "uv installation failed; see https://docs.astral.sh/uv/"
  ok "uv installed"
}

install_just() {
  if have just; then ok "just $(just --version | awk '{print $2}')"; return 0; fi
  log "installing just"
  if is_macos && have brew; then
    brew install just
  elif is_linux && have apt-get; then
    sudo apt-get install -y just >/dev/null 2>&1 || warn "apt has no 'just'; install from https://just.systems"
  fi
  have just && ok "just installed" || warn "just not installed (recipes can still be run by hand)"
}

install_node() {
  if have node; then ok "node $(node --version)"; return 0; fi
  log "installing node"
  if is_macos && have brew; then
    brew install node
  elif is_linux && have apt-get; then
    sudo apt-get install -y nodejs npm >/dev/null 2>&1 || warn "install Node ${SIO_NODE_MAJOR:-22} manually"
  fi
  have node && ok "node $(node --version)" || warn "node missing: the web UI will not build"
}

# ------------------------------------------------------------------- datastores: macOS
brew_install() {
  formula="$1"
  if brew list --formula "${formula}" >/dev/null 2>&1; then
    ok "${formula} (already installed)"
  else
    log "brew install ${formula}"
    brew install "${formula}"
    ok "${formula}"
  fi
}

bootstrap_macos_datastores() {
  have brew || die "Homebrew is required on macOS: https://brew.sh"
  brew_install "${SIO_BREW_POSTGRES}"
  brew_install "${SIO_BREW_POSTGIS}"
  brew_install "${SIO_BREW_PGVECTOR}"
  brew_install "${SIO_BREW_REDIS}"
  if [ "${PROFILE}" != "minimal" ]; then
    brew_install "${SIO_BREW_NEO4J}"
    brew_install "${SIO_BREW_MINIO}"
    brew_install "${SIO_BREW_TEMPORAL}"
  fi
  if [ "${PROFILE}" = "full" ]; then
    brew_install "${SIO_BREW_OLLAMA}"
    brew_install "${SIO_BREW_GRAFANA}"
    brew_install "${SIO_BREW_GSTREAMER}"
  fi
}

# ------------------------------------------------------------------- datastores: Linux
apt_install() {
  missing=""
  for pkg in "$@"; do
    if ! dpkg -s "${pkg}" >/dev/null 2>&1; then
      missing="${missing} ${pkg}"
    fi
  done
  if [ -z "${missing}" ]; then
    ok "apt packages already present:$*"
    return 0
  fi
  log "apt-get install${missing}"
  sudo apt-get update -qq
  # shellcheck disable=SC2086
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ${missing} >/dev/null
  ok "installed${missing}"
}

install_neo4j_tarball() {
  target="${SIO_STATE_DIR}/neo4j"
  if [ -x "${target}/bin/neo4j" ]; then
    ok "neo4j ${SIO_NEO4J_VERSION} (already installed under .sio/neo4j)"
    return 0
  fi
  log "downloading neo4j ${SIO_NEO4J_VERSION}"
  have java || apt_install default-jre-headless
  archive="${SIO_STATE_DIR}/neo4j.tar.gz"
  download "${SIO_NEO4J_TARBALL_URL}" "${archive}"
  rm -rf "${target}" && mkdir -p "${target}"
  tar -xzf "${archive}" -C "${target}" --strip-components=1
  rm -f "${archive}"
  ok "neo4j installed under .sio/neo4j"
}

install_minio_binary() {
  if [ -x "${SIO_BIN_DIR}/minio" ]; then ok "minio (already installed)"; return 0; fi
  log "downloading minio"
  url="${SIO_MINIO_URL_LINUX}"
  is_macos && url="${SIO_MINIO_URL_DARWIN}"
  download "${url}" "${SIO_BIN_DIR}/minio"
  chmod +x "${SIO_BIN_DIR}/minio"
  ok "minio installed under .sio/bin"
}

install_temporal_binary() {
  if [ -x "${SIO_BIN_DIR}/temporal" ]; then ok "temporal (already installed)"; return 0; fi
  log "downloading temporal cli"
  os="linux"; arch="amd64"
  is_macos && os="darwin"
  case "$(uname -m)" in arm64|aarch64) arch="arm64" ;; esac
  url="https://temporal.download/cli/archive/v${SIO_TEMPORAL_VERSION}?platform=${os}&arch=${arch}"
  archive="${SIO_STATE_DIR}/temporal.tar.gz"
  if download "${url}" "${archive}"; then
    tar -xzf "${archive}" -C "${SIO_BIN_DIR}" temporal 2>/dev/null || tar -xzf "${archive}" -C "${SIO_BIN_DIR}"
    rm -f "${archive}"
    chmod +x "${SIO_BIN_DIR}/temporal" 2>/dev/null || true
    ok "temporal cli installed under .sio/bin"
  else
    warn "temporal cli download failed; workflows will fall back to SIO_WORKFLOW_RUNNER=inline"
  fi
}

install_ollama_linux() {
  if have ollama; then ok "ollama $(ollama --version 2>/dev/null | head -n1)"; return 0; fi
  log "installing ollama"
  download "https://ollama.com/install.sh" "${SIO_STATE_DIR}/ollama-install.sh"
  sh "${SIO_STATE_DIR}/ollama-install.sh" >/dev/null 2>&1 ||
    warn "ollama install failed; the copilot will need SIO_LLM_PROVIDER=scripted or a remote model"
  have ollama && ok "ollama installed"
}

bootstrap_linux_datastores() {
  have apt-get || die "this Linux path expects apt-get (Debian/Ubuntu)"
  apt_install "${SIO_APT_POSTGRES}" "${SIO_APT_POSTGIS}" "${SIO_APT_PGVECTOR}" "${SIO_APT_REDIS}"
  if [ "${PROFILE}" != "minimal" ]; then
    install_neo4j_tarball
    install_minio_binary
    install_temporal_binary
  fi
  if [ "${PROFILE}" = "full" ]; then
    install_ollama_linux
  fi
}

# ------------------------------------------------------------------------ dependencies
install_python_deps() {
  log "installing python dependencies (uv sync)"
  UV="$(uv_bin)"
  (cd "${SIO_ROOT}" && "${UV}" sync)
  ok "python workspace synced"
}

install_web_deps() {
  if [ ! -f "${SIO_ROOT}/web/package.json" ]; then
    info "web/ not present yet — skipping npm install"
    return 0
  fi
  have npm || { warn "npm missing; skipping web dependencies"; return 0; }
  log "installing web dependencies (npm install)"
  (cd "${SIO_ROOT}/web" && npm install --no-fund --no-audit)
  ok "web dependencies installed"
}

# ------------------------------------------------------------------------------- main
if [ "${PROFILE}" != "deps" ]; then
  install_uv
  install_just
  install_node
  if is_macos; then
    bootstrap_macos_datastores
  elif is_linux; then
    bootstrap_linux_datastores
  else
    warn "unsupported platform $(uname -s); install datastores manually and re-run --deps"
  fi
else
  install_uv
fi

install_python_deps
install_web_deps

log "bootstrap complete"
info "next:  just services    # start postgres, redis, neo4j, minio"
info "then:  just doctor      # verify everything, including datastore initialisation"
