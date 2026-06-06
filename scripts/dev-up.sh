#!/usr/bin/env bash
# EE Gateway — bring up the local development stack.
# Copyright (C) 2026 encryptedenergy.com
# SPDX-License-Identifier: GPL-3.0-only
#
# Builds both containers from source and runs them via docker-compose.dev.yml.
# Open http://localhost:8080 for the setup wizard and dashboard.
#
# Usage:
#   scripts/dev-up.sh            # build + run in the foreground (Ctrl-C to stop)
#   scripts/dev-up.sh -d         # build + run detached
#   scripts/dev-up.sh --build    # force a rebuild (already implied below)
# Any extra arguments are passed straight through to `docker compose up`.
#
# To stop and remove a detached stack:
#   docker compose -f docker-compose.dev.yml down

set -euo pipefail

# Resolve the repo root from this script's location so it runs from anywhere.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/docker-compose.dev.yml"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed or not on PATH" >&2
  exit 1
fi

# Prefer the Compose v2 plugin (`docker compose`); fall back to legacy
# `docker-compose` if that is what is installed.
if docker compose version >/dev/null 2>&1; then
  compose() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  compose() { docker-compose "$@"; }
else
  echo "error: neither 'docker compose' nor 'docker-compose' is available" >&2
  exit 1
fi

echo "EE Gateway dev stack — building and starting (UI on http://localhost:8080)"
exec compose -f "$COMPOSE_FILE" up --build "$@"
