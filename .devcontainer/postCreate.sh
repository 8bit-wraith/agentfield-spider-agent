#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &> /dev/null && pwd)"
cd "$ROOT_DIR"

export SPIDER_INSTALL_DOCKER_STRICT="${SPIDER_INSTALL_DOCKER_STRICT:-0}"

bash .environment/setup.sh
