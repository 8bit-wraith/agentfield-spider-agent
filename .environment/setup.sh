#!/usr/bin/env bash
# Shared environment bootstrap for cloud agents + local devcontainers.

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &> /dev/null && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
PIP_CMD=("$PYTHON_BIN" -m pip)

echo "[env] using python: $("$PYTHON_BIN" --version 2>&1)"
echo "[env] upgrading pip/setuptools/wheel"
"${PIP_CMD[@]}" install --upgrade pip setuptools wheel

echo "[env] installing project dependencies from requirements.txt"
"${PIP_CMD[@]}" install -r requirements.txt

if [[ "${SPIDER_INSTALL_DOCLING:-0}" == "1" ]]; then
    echo "[env] SPIDER_INSTALL_DOCLING=1 -> installing docling extra"
    "${PIP_CMD[@]}" install "docling>=2.0"
fi

echo "[env] validating dependency graph with pip check"
"${PIP_CMD[@]}" check

echo "[env] setup complete"
