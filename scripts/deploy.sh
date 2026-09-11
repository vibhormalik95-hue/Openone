#!/usr/bin/env bash
# Run on the deployment host from a trusted checkout. Migrations must be additive.
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")/.."
[[ $# == 1 ]] || { echo 'Usage: deploy.sh ghcr.io/OWNER/IMAGE@sha256:DIGEST' >&2; exit 2; }
image="$1"
[[ "$image" =~ ^ghcr\.io/[a-z0-9_./-]+@sha256:[a-f0-9]{64}$ ]] || { echo 'An immutable GHCR digest is required.' >&2; exit 2; }
exec 9>deploy/deploy.lock
flock -n 9 || { echo 'Another deployment is active.' >&2; exit 1; }
source deploy/host.env
source deploy/state.env
old_color="$ACTIVE_COLOR"
if [[ "$old_color" == blue ]]; then new_color=green; else new_color=blue; fi
cp deploy/state.env deploy/state.rollback.env
cp deploy/caddy/Caddyfile deploy/caddy/Caddyfile.rollback
switched=0
recover() {
  code=$?
  trap - ERR INT TERM HUP
  set +e
  if [[ "$code" == 0 ]]; then code=1; fi
  cp deploy/state.rollback.env deploy/state.env
  cp deploy/caddy/Caddyfile.rollback deploy/caddy/Caddyfile
  chmod 0644 deploy/caddy/Caddyfile
  if [[ "$old_color" != none && "$switched" == 1 ]]; then
    scripts/compose.sh up -d --no-deps --wait --wait-timeout 60 "$old_color"
    printf 'to %s:8000\n' "$old_color" > deploy/caddy/upstream.conf.tmp
    mv deploy/caddy/upstream.conf.tmp deploy/caddy/upstream.conf
    chmod 0644 deploy/caddy/upstream.conf
    scripts/compose.sh exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
    scripts/compose.sh up -d --no-deps worker
  fi
  scripts/compose.sh stop "$new_color"
  echo 'Deploy failed; previous application state restored. Additive database migrations remain applied.' >&2
  exit "$code"
}
trap recover ERR INT TERM HUP
docker pull "$image"
# Verify OCI release before changing traffic; credentials are registry-scoped.
docker image inspect "$image" >/dev/null
scripts/migrate.sh
if [[ ${COMPOSE_EXTRA:-} == compose.oauth.yml ]]; then
  scripts/compose.sh up -d --wait --wait-timeout 60 oauth-redis
fi
export HVM_NEW_COLOR="$new_color" HVM_NEW_IMAGE="$image"
python3 - <<'PY'
import os
from pathlib import Path
p=Path('deploy/state.env'); text=p.read_text()
key='APP_IMAGE_'+os.environ['HVM_NEW_COLOR'].upper()
text=''.join(f"{key}='{os.environ['HVM_NEW_IMAGE']}'\n" if line.startswith(key+'=') else line for line in text.splitlines(True))
p.write_text(text)
PY
scripts/compose.sh up -d --no-deps --wait --wait-timeout 90 "$new_color"
scripts/compose.sh exec -T "$new_color" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3)"
if [[ -s deploy/secrets/deploy_smoke_token && -s deploy/secrets/deploy_smoke_project_id ]]; then
  MCP_TOKEN=$(<deploy/secrets/deploy_smoke_token)
  HVM_SMOKE_PROJECT_ID=$(<deploy/secrets/deploy_smoke_project_id)
  export MCP_TOKEN HVM_SMOKE_PROJECT_ID
  scripts/compose.sh exec -T -e HVM_BASE_URL=http://127.0.0.1:8000 -e HVM_ALLOW_LOOPBACK=true \
    -e MCP_TOKEN -e HVM_SMOKE_PROJECT_ID "$new_color" python - < scripts/smoke.py
  unset MCP_TOKEN HVM_SMOKE_PROJECT_ID
elif [[ "$old_color" != none ]]; then
  echo 'Release blocked: configure deploy/secrets/deploy_smoke_token and deploy_smoke_project_id before updating an existing service.' >&2
  false
else
  echo 'First bootstrap only: authenticated candidate smoke skipped until a canary key/project is provisioned.' >&2
fi
cp "${HVM_CADDY_SOURCE:-Caddyfile}" deploy/caddy/Caddyfile.tmp
mv deploy/caddy/Caddyfile.tmp deploy/caddy/Caddyfile
chmod 0644 deploy/caddy/Caddyfile
printf 'to %s:8000\n' "$new_color" > deploy/caddy/upstream.conf.tmp
mv deploy/caddy/upstream.conf.tmp deploy/caddy/upstream.conf
chmod 0644 deploy/caddy/upstream.conf
switched=1
if [[ -n $(scripts/compose.sh ps -q --status running caddy) ]]; then
  scripts/compose.sh exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  scripts/compose.sh exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
else
  scripts/compose.sh up -d --no-deps caddy
fi
# Certificate issuance can need time on the first deployment.
curl --fail --silent --show-error --retry 12 --retry-delay 3 --retry-all-errors --max-time 10 "https://$DOMAIN/health/ready" -o /dev/null
python3 - <<'PY'
import os
from pathlib import Path
p=Path('deploy/state.env'); values={'ACTIVE_COLOR':os.environ['HVM_NEW_COLOR'],'WORKER_IMAGE':os.environ['HVM_NEW_IMAGE']}
p.write_text(''.join(f"{k}='{values[k]}'\n" if (k:=line.split('=',1)[0]) in values else line for line in p.read_text().splitlines(True)))
PY
# Stop old worker before starting replacement; outstanding jobs use database leases.
scripts/compose.sh up -d --no-deps worker
[[ -n $(scripts/compose.sh ps -q --status running worker) ]]
cp deploy/state.rollback.env deploy/previous.env
cp deploy/caddy/Caddyfile.rollback deploy/previous.Caddyfile
if [[ "$old_color" != none ]]; then
  printf 'Traffic switched; allowing 45 seconds for old requests to drain.\n'
  sleep 45
  scripts/compose.sh stop "$old_color"
fi
trap - ERR INT TERM HUP
printf 'Deployed %s to %s.\n' "$image" "$new_color"
