#!/usr/bin/env bash
# Probe HTTP/TLS connection reuse across aiohttp + browser backends.
#
# Defaults:
#   targets  = https://www.howsmyssl.com/a/check + https://httpbin.org/headers
#   backends = cloak,lightpanda
#
# Overrides:
#   SMOKE_REUSE_TARGETS="https://httpbin.org/headers,https://www.howsmyssl.com/a/check"
#   SMOKE_REUSE_BACKENDS="cloak,lightpanda"
#   SMOKE_REUSE_REQUIRE_BACKENDS="cloak"   # fail if skipped
#   SMOKE_REUSE_ENFORCE_TARGETS="https://httpbin.org/headers"

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

TARGETS="${SMOKE_REUSE_TARGETS:-https://www.howsmyssl.com/a/check,https://httpbin.org/headers}"
BACKENDS="${SMOKE_REUSE_BACKENDS:-cloak,lightpanda}"
REQUIRED="${SMOKE_REUSE_REQUIRE_BACKENDS:-}"
ENFORCE_TARGETS="${SMOKE_REUSE_ENFORCE_TARGETS:-https://httpbin.org/headers}"

echo "═══ G. Network reuse probe (aiohttp + browser pools) ═══"

args=()
IFS=',' read -r -a target_arr <<< "$TARGETS"
for t in "${target_arr[@]}"; do
    [[ -n "$t" ]] && args+=(--target "$t")
done

IFS=',' read -r -a backend_arr <<< "$BACKENDS"
for b in "${backend_arr[@]}"; do
    [[ -n "$b" ]] && args+=(--backend "$b")
done

IFS=',' read -r -a req_arr <<< "$REQUIRED"
for b in "${req_arr[@]}"; do
    [[ -n "$b" ]] && args+=(--require-backend "$b")
done

IFS=',' read -r -a enforce_arr <<< "$ENFORCE_TARGETS"
for t in "${enforce_arr[@]}"; do
    [[ -n "$t" ]] && args+=(--enforce-target "$t")
done

python3 "$SCRIPT_DIR/probe_network_reuse.py" "${args[@]}"
