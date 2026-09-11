#!/usr/bin/env bash
# Rolls application/worker back; never automatically reverses database migrations.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ -r deploy/previous.env ]] || { echo 'No previous successful deployment is recorded.' >&2; exit 1; }
source deploy/previous.env
case "$ACTIVE_COLOR" in
  blue) previous="$APP_IMAGE_BLUE" ;;
  green) previous="$APP_IMAGE_GREEN" ;;
  *) echo 'No earlier release to restore.' >&2; exit 1 ;;
esac
export HVM_CADDY_SOURCE=deploy/previous.Caddyfile
exec scripts/deploy.sh "$previous"
