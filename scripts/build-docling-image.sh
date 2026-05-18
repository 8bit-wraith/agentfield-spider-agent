#!/usr/bin/env bash
# Build + push Docling-enabled spider image with Docker Buildx cloud.
#
# Requires:
#   DOCKER_USER=<dockerhub-user>
#   docker login already completed (or configure DOCKER_PAT and login manually)
#
# Optional:
#   BUILDX_ENDPOINT=60ph3r/main-gh-builder-joe
#   IMAGE_REPO=agentfield-spider-agent
#   IMAGE_TAG=docling-latest

set -euo pipefail

DOCKER_USER="${DOCKER_USER:-}"
if [[ -z "$DOCKER_USER" ]]; then
    echo "DOCKER_USER is required" >&2
    exit 1
fi

BUILDX_ENDPOINT="${BUILDX_ENDPOINT:-60ph3r/main-gh-builder-joe}"
IMAGE_REPO="${IMAGE_REPO:-agentfield-spider-agent}"
IMAGE_TAG="${IMAGE_TAG:-docling-latest}"
IMAGE_REF="${DOCKER_USER}/${IMAGE_REPO}:${IMAGE_TAG}"

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
ROOT_DIR="$( cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd )"
cd "$ROOT_DIR"

BUILDER_NAME="docling-cloud-builder"

if ! docker buildx inspect "$BUILDER_NAME" > /dev/null 2>&1; then
    docker buildx create --name "$BUILDER_NAME" --driver cloud "$BUILDX_ENDPOINT"
fi
docker buildx use "$BUILDER_NAME"

docker buildx build \
    --push \
    --build-arg INSTALL_DOCLING=1 \
    --tag "$IMAGE_REF" \
    .

echo "pushed_image=${IMAGE_REF}"
