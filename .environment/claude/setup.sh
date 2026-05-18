#!/usr/bin/env bash
# Claude environment bootstrap.

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." &> /dev/null && pwd)"
exec "$ROOT_DIR/.environment/setup.sh"
