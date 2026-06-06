#!/usr/bin/env bash
# EE Gateway — build and publish multi-arch container images.
# Copyright (C) 2026 encryptedenergy.com
# SPDX-License-Identifier: GPL-3.0-only
#
# Builds the worker and UI images for linux/arm64 (Raspberry Pi / Umbrel Home)
# and linux/amd64, then pushes them to Docker Hub. Multi-arch manifests can only
# be published, not loaded into the local daemon, so this script always pushes;
# use scripts/dev-up.sh for local single-arch build-and-run instead.
#
# Prerequisites:
#   * Docker with the buildx plugin.
#   * `docker login` to an account with push access to the target org.
#
# Usage:
#   scripts/build.sh                 # build + push version 0.1.0 and :latest
#   scripts/build.sh 0.2.0           # build + push a specific version
#   EE_DOCKER_ORG=myorg scripts/build.sh
#   EE_PLATFORMS=linux/arm64 scripts/build.sh   # narrow the platform list
#
# Environment overrides:
#   EE_DOCKER_ORG  Docker Hub org / namespace          (default: encryptedenergy)
#   EE_PLATFORMS   comma-separated buildx platforms     (default: linux/arm64,linux/amd64)
#   EE_BUILDER     buildx builder name to create/use    (default: ee-gateway-builder)

set -euo pipefail

VERSION="${1:-0.1.0}"
ORG="${EE_DOCKER_ORG:-encryptedenergy}"
PLATFORMS="${EE_PLATFORMS:-linux/arm64,linux/amd64}"
BUILDER="${EE_BUILDER:-ee-gateway-builder}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed or not on PATH" >&2
  exit 1
fi
if ! docker buildx version >/dev/null 2>&1; then
  echo "error: docker buildx is not available (install the buildx plugin)" >&2
  exit 1
fi

# Create the builder on first run, otherwise reuse it. A dedicated builder keeps
# the QEMU emulators for cross-arch builds isolated from the default context.
if docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  docker buildx use "$BUILDER"
else
  echo "creating buildx builder: $BUILDER"
  docker buildx create --name "$BUILDER" --use
fi
docker buildx inspect --bootstrap >/dev/null

echo "Building EE Gateway $VERSION for [$PLATFORMS] -> $ORG (push)"

# build_image <context-subdir> <repo-name>
build_image() {
  local context="$1"
  local repo="$2"
  echo
  echo "==> $ORG/$repo:$VERSION"
  docker buildx build \
    --platform "$PLATFORMS" \
    --tag "$ORG/$repo:$VERSION" \
    --tag "$ORG/$repo:latest" \
    --push \
    "$ROOT/$context"
}

build_image worker ee-gateway-worker
build_image ui ee-gateway-ui

echo
echo "Done. Published:"
echo "  $ORG/ee-gateway-worker:$VERSION (and :latest)"
echo "  $ORG/ee-gateway-ui:$VERSION (and :latest)"
