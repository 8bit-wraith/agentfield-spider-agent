#!/usr/bin/env bash
# Minimal validation path for .environment/setup.sh branching logic.

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
ROOT_DIR="$( cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd )"
cd "$ROOT_DIR"

echo "═══ A. syntax check (.environment/setup.sh) ═══"
bash -n .environment/setup.sh

echo "═══ B. dry-run: non-strict no-elevation path should pass ═══"
SPIDER_SETUP_DRY_RUN=1 \
SPIDER_INSTALL_DOCKER=1 \
SPIDER_FORCE_NO_ELEVATION=1 \
SPIDER_FORCE_DOCKER_MISSING=1 \
SPIDER_INSTALL_DOCKER_STRICT=0 \
bash .environment/setup.sh > /dev/null

echo "═══ C. dry-run: strict no-elevation path should fail ═══"
if SPIDER_SETUP_DRY_RUN=1 \
   SPIDER_INSTALL_DOCKER=1 \
   SPIDER_FORCE_NO_ELEVATION=1 \
   SPIDER_FORCE_DOCKER_MISSING=1 \
   SPIDER_INSTALL_DOCKER_STRICT=1 \
   bash .environment/setup.sh > /dev/null 2>&1; then
    echo "expected strict mode failure but setup succeeded" >&2
    exit 2
fi

echo "✓ env bootstrap checks passed"
