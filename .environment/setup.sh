#!/usr/bin/env bash
# Shared environment bootstrap for cloud agents + local devcontainers.

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &> /dev/null && pwd)"
cd "$ROOT_DIR"

_docker_arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo "amd64" ;;
        aarch64|arm64) echo "arm64" ;;
        *)
            echo "unsupported-arch"
            ;;
    esac
}

_docker_warn_or_fail() {
    local message="$1"

    if [[ "${SPIDER_INSTALL_DOCKER_STRICT:-0}" == "1" ]]; then
        echo "[env] ERROR: ${message}" >&2
        return 1
    fi

    echo "[env] WARN: ${message}; continuing without docker bootstrap" >&2
    return 0
}

_detect_privilege_mode() {
    local forced_mode
    forced_mode="${SPIDER_SETUP_PRIVILEGE_MODE:-auto}"
    case "$forced_mode" in
        root|sudo|none)
            echo "$forced_mode"
            return
            ;;
        auto|"")
            ;;
        *)
            echo "[env] WARN: ignoring invalid SPIDER_SETUP_PRIVILEGE_MODE=${forced_mode}" >&2
            ;;
    esac

    if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        echo "root"
        return
    fi

    if command -v sudo > /dev/null 2>&1 && sudo -n true > /dev/null 2>&1; then
        echo "sudo"
        return
    fi

    echo "none"
}

_apt_get() {
    local privilege_mode="$1"
    shift

    case "$privilege_mode" in
        root)
            apt-get "$@"
            ;;
        sudo)
            sudo -n apt-get "$@"
            ;;
        *)
            echo "[env] ERROR: apt-get requested without elevation capability" >&2
            return 1
            ;;
    esac
}

_ensure_docker_cli() {
    if command -v docker > /dev/null 2>&1; then
        echo "[env] docker cli already present: $(docker --version 2>/dev/null || echo docker)"
        return
    fi

    local arch
    arch="$(_docker_arch)"
    if [[ "$arch" == "unsupported-arch" ]]; then
        echo "[env] unsupported architecture for docker install: $(uname -m)" >&2
        return 1
    fi

    local privilege_mode
    privilege_mode="$(_detect_privilege_mode)"
    case "$privilege_mode" in
        root)
            echo "[env] installing docker cli via apt (root)"
            ;;
        sudo)
            echo "[env] installing docker cli via apt (sudo -n)"
            ;;
        none)
            _docker_warn_or_fail "docker install requested but no root access or passwordless sudo is available"
            return
            ;;
    esac

    _apt_get "$privilege_mode" update
    if ! _apt_get "$privilege_mode" install -y --no-install-recommends docker.io docker-buildx docker-compose-v2; then
        echo "[env] docker-buildx/docker-compose-v2 unavailable; installing docker.io only"
        _apt_get "$privilege_mode" install -y --no-install-recommends docker.io
    fi
}

_ensure_buildx_plugin() {
    if ! command -v docker > /dev/null 2>&1; then
        _docker_warn_or_fail "docker cli not found; skipping buildx plugin install"
        return
    fi

    if docker buildx version > /dev/null 2>&1; then
        echo "[env] docker buildx already available"
        return
    fi

    local arch
    arch="$(_docker_arch)"
    if [[ "$arch" == "unsupported-arch" ]]; then
        echo "[env] unsupported architecture for buildx install: $(uname -m)" >&2
        return 1
    fi

    local plugin_dir
    plugin_dir="${HOME}/.docker/cli-plugins"
    mkdir -p "$plugin_dir"

    local buildx_url
    buildx_url="https://github.com/docker/buildx/releases/latest/download/buildx-linux-${arch}"
    echo "[env] installing docker buildx plugin from ${buildx_url}"
    curl -fsSL "$buildx_url" -o "${plugin_dir}/docker-buildx"
    chmod +x "${plugin_dir}/docker-buildx"
}

_docker_login_if_configured() {
    local user registry
    user="${DOCKER_USER:-}"
    registry="${DOCKER_REGISTRY:-docker.io}"

    if ! command -v docker > /dev/null 2>&1; then
        _docker_warn_or_fail "docker cli not found; skipping docker login"
        return
    fi

    if [[ -z "${DOCKER_PAT:-}" || -z "$user" ]]; then
        echo "[env] docker login skipped (set DOCKER_USER + DOCKER_PAT to enable)"
        return
    fi

    echo "[env] docker login -> ${registry} as ${user}"
    echo "${DOCKER_PAT}" | docker login "${registry}" --username "${user}" --password-stdin
}

PYTHON_BIN="${PYTHON_BIN:-python3}"
PIP_CMD=("$PYTHON_BIN" -m pip)
SPIDER_INSTALL_DOCKER="${SPIDER_INSTALL_DOCKER:-1}"
SPIDER_INSTALL_DOCKER_STRICT="${SPIDER_INSTALL_DOCKER_STRICT:-0}"

if [[ "$SPIDER_INSTALL_DOCKER" == "1" ]]; then
    echo "[env] docker bootstrap enabled (strict=${SPIDER_INSTALL_DOCKER_STRICT})"
    _ensure_docker_cli
    _ensure_buildx_plugin
    _docker_login_if_configured
else
    echo "[env] docker install/login disabled (SPIDER_INSTALL_DOCKER=${SPIDER_INSTALL_DOCKER})"
fi

echo "[env] using python: $("$PYTHON_BIN" --version 2>&1)"
echo "[env] upgrading pip/setuptools/wheel"
"${PIP_CMD[@]}" install --upgrade pip setuptools wheel

echo "[env] installing project dependencies from requirements.txt"
"${PIP_CMD[@]}" install -r requirements.txt

install_docling=0
if [[ "${SPIDER_INSTALL_DOCLING:-0}" == "1" || "${SPIDER_DOCLING_ENABLED:-0}" == "1" ]]; then
    install_docling=1
fi

if [[ "$install_docling" == "1" ]]; then
    if [[ "${SPIDER_DOCLING_ENABLED:-0}" == "1" && "${SPIDER_INSTALL_DOCLING:-0}" != "1" ]]; then
        echo "[env] SPIDER_DOCLING_ENABLED is deprecated; prefer SPIDER_INSTALL_DOCLING=1"
    fi
    echo "[env] docling requested -> installing docling extra"
    "${PIP_CMD[@]}" install "docling>=2.0"
fi

echo "[env] validating dependency graph with pip check"
"${PIP_CMD[@]}" check

echo "[env] setup complete"
