#!/usr/bin/env bash
# Smart build for browser engines (lightpanda + cloakbrowser).
#
# Checks the upstream release vs the tag already in our private
# registry. If upstream is newer, builds + pushes a new image and
# updates the :latest pointer. If they match, prints the existing
# image ref and exits 0 — no work done.
#
# Usage:
#
#   scripts/build_browser.sh lightpanda
#   scripts/build_browser.sh cloak
#   scripts/build_browser.sh lightpanda --force          # rebuild even if up to date
#   scripts/build_browser.sh lightpanda --mode=source    # build from source (lightpanda only)
#
# Environment:
#
#   REGISTRY       (required)  e.g. ghcr.io/peterlodri-sec
#   GH_TOKEN       (optional)  used for GitHub API + registry push
#   PLATFORMS      (optional)  buildx platforms, default linux/amd64
#
# Exit codes:
#   0   built + pushed, OR already up to date
#   1   bad args / required env missing
#   2   upstream API failure
#   3   build failed
#   4   push failed

set -euo pipefail

# ─── args ───────────────────────────────────────────────────────────
ENGINE="${1:-}"
shift || true

FORCE=0
MODE=binary

for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --mode=*) MODE="${arg#--mode=}" ;;
        --help|-h)
            sed -n '1,30p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 1
            ;;
    esac
done

if [[ -z "$ENGINE" || ( "$ENGINE" != "lightpanda" && "$ENGINE" != "cloak" ) ]]; then
    echo "usage: $0 <lightpanda|cloak> [--force] [--mode=binary|source]" >&2
    exit 1
fi

REGISTRY="${REGISTRY:-}"
if [[ -z "$REGISTRY" ]]; then
    echo "REGISTRY env required (e.g. ghcr.io/peterlodri-sec)" >&2
    exit 1
fi

PLATFORMS="${PLATFORMS:-linux/amd64}"
IMAGE_NAME="${ENGINE}-opt-ag"
IMAGE_BASE="${REGISTRY}/${IMAGE_NAME}"

# ─── per-engine upstream version probe ──────────────────────────────
get_upstream_version() {
    case "$ENGINE" in
        lightpanda)
            # Lightpanda's "latest" release is the `nightly` tag —
            # they don't ship semver releases. We use the published_at
            # timestamp of the nightly tag as a stable version key.
            local published_at
            published_at=$(gh release view nightly \
                --repo lightpanda-io/browser \
                --json publishedAt -q .publishedAt 2>/dev/null || echo "")
            if [[ -z "$published_at" ]]; then
                echo "could not fetch lightpanda upstream version" >&2
                return 2
            fi
            # Tag-friendly: 2026-05-18T14:30:00Z -> nightly-20260518
            echo "nightly-$(date -d "$published_at" -u +%Y%m%d 2>/dev/null \
                            || echo "$published_at" | tr -dc '0-9' | head -c 8)"
            ;;
        cloak)
            # Cloakbrowser ships via PyPI; the version IS the PyPI
            # release. Fetch from the JSON API.
            local v
            v=$(curl -fsSL https://pypi.org/pypi/cloakbrowser/json \
                 | python3 -c "import sys, json; print(json.load(sys.stdin)['info']['version'])" \
                 2>/dev/null || echo "")
            if [[ -z "$v" ]]; then
                echo "could not fetch cloakbrowser upstream version" >&2
                return 2
            fi
            echo "$v"
            ;;
    esac
}

# ─── registry probe ─────────────────────────────────────────────────
# Returns the version label on the current :latest image, or empty if
# no :latest exists yet.
get_registry_version() {
    # We tag every built image with `version=<UPSTREAM_VERSION>` as a
    # docker label so we can interrogate it later without manifest
    # decoding gymnastics.
    docker manifest inspect "${IMAGE_BASE}:latest" 2>/dev/null \
        | python3 -c "
import sys, json
try:
    m = json.load(sys.stdin)
    # OCI manifest list -> dig for v annotation; fall back to none.
    print(m.get('annotations', {}).get('org.opencontainers.image.version', ''))
except Exception:
    pass
" || echo ""
}

# ─── decide + build ─────────────────────────────────────────────────
echo "== ${ENGINE} build flow =="
echo "registry:  ${REGISTRY}"
echo "image:     ${IMAGE_BASE}"
echo "platforms: ${PLATFORMS}"
echo "mode:      ${MODE}"

UPSTREAM_VERSION=$(get_upstream_version) || {
    rc=$?
    echo "upstream probe failed (rc=$rc)" >&2
    exit $rc
}
echo "upstream:  ${UPSTREAM_VERSION}"

REGISTRY_VERSION=$(get_registry_version) || REGISTRY_VERSION=""
echo "registry version: ${REGISTRY_VERSION:-<none>}"

if [[ "$FORCE" -eq 0 && -n "$REGISTRY_VERSION" && "$REGISTRY_VERSION" == "$UPSTREAM_VERSION" ]]; then
    echo "✓ up to date — using existing ${IMAGE_BASE}:latest"
    echo "image_ref=${IMAGE_BASE}:latest"
    echo "version=${UPSTREAM_VERSION}"
    exit 0
fi

echo "→ build required (force=${FORCE}, upstream=${UPSTREAM_VERSION}, registry=${REGISTRY_VERSION:-none})"

# Pick the right Dockerfile.
DOCKERFILE=""
BUILD_ARGS=()
case "$ENGINE" in
    lightpanda)
        DOCKERFILE="Dockerfile.lightpanda"
        BUILD_ARGS+=(--build-arg "BUILD_MODE=${MODE}")
        BUILD_ARGS+=(--build-arg "LIGHTPANDA_REF=nightly")
        ;;
    cloak)
        DOCKERFILE="Dockerfile.cloak"
        BUILD_ARGS+=(--build-arg "CLOAKBROWSER_VERSION=${UPSTREAM_VERSION}")
        ;;
esac

# Use buildx for multi-platform + push in one step.
docker buildx build \
    --platform "${PLATFORMS}" \
    -f "${DOCKERFILE}" \
    "${BUILD_ARGS[@]}" \
    --label "org.opencontainers.image.version=${UPSTREAM_VERSION}" \
    --label "org.opencontainers.image.source=https://github.com/peterlodri-sec/agentfield-spider-agent" \
    --label "org.opencontainers.image.title=${IMAGE_NAME}" \
    -t "${IMAGE_BASE}:${UPSTREAM_VERSION}" \
    -t "${IMAGE_BASE}:latest" \
    --push \
    . || {
    echo "✗ build/push failed" >&2
    exit 3
}

echo "✓ built + pushed"
echo "image_ref=${IMAGE_BASE}:${UPSTREAM_VERSION}"
echo "image_ref_latest=${IMAGE_BASE}:latest"
echo "version=${UPSTREAM_VERSION}"
