#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
source deploy/host.env
args=(--env-file deploy/versions.env --env-file deploy/host.env --env-file deploy/state.env -f docker-compose.prod.yml)
if [[ -n ${COMPOSE_EXTRA:-} ]]; then args+=(-f "$COMPOSE_EXTRA"); fi
exec docker compose "${args[@]}" "$@"
