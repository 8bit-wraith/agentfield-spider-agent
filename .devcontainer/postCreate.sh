#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &> /dev/null && pwd)"
cd "$ROOT_DIR"

# Devcontainer shells typically don't need Docker-in-Docker unless
# explicitly requested.
export SPIDER_INSTALL_DOCKER="${SPIDER_INSTALL_DOCKER:-0}"
bash .environment/setup.sh
