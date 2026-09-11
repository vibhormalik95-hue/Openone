#!/usr/bin/env bash
# Usage: scripts/bootstrap.sh DNS_NAME TLS_EMAIL APP_IMAGE@sha256:DIGEST [selfhost|managed]
# Requires Docker Engine + Compose plugin, python3, and DNS pointing at this host.
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")/.."
[[ $# -ge 3 && $# -le 4 ]] || { echo 'Usage: bootstrap.sh DNS_NAME TLS_EMAIL IMAGE@sha256:DIGEST [selfhost|managed]' >&2; exit 2; }
export HVM_DOMAIN="$1" HVM_ACME_EMAIL="$2" HVM_INITIAL_IMAGE="$3" HVM_DB_MODE="${4:-selfhost}"
[[ "$HVM_DOMAIN" =~ ^[A-Za-z0-9][A-Za-z0-9.-]+$ ]] || { echo 'Use a DNS hostname, without protocol/path.' >&2; exit 2; }
[[ "$HVM_INITIAL_IMAGE" =~ ^ghcr\.io/[a-z0-9_./-]+@sha256:[a-f0-9]{64}$ ]] || { echo 'Image must be an immutable GHCR digest.' >&2; exit 2; }
[[ "$HVM_DB_MODE" == selfhost || "$HVM_DB_MODE" == managed ]] || exit 2
[[ ! -e deploy/host.env ]] || { echo 'Already initialized; edit existing protected config. Bootstrap never rotates credentials silently.' >&2; exit 1; }
command -v docker >/dev/null
command -v python3 >/dev/null
docker compose version >/dev/null
mkdir -p deploy/secrets deploy/caddy backups
# Tags select a reviewed series once; RepoDigests record the actual deployable bytes.
resolve() {
  docker pull "$1" >&2
  docker image inspect --format '{{index .RepoDigests 0}}' "$1"
}
export HVM_POSTGRES_IMAGE HVM_CADDY_IMAGE HVM_PYTHON_IMAGE
HVM_POSTGRES_IMAGE=$(resolve pgvector/pgvector:0.8.6-pg17-bookworm)
HVM_CADDY_IMAGE=$(resolve caddy:2-alpine)
HVM_PYTHON_IMAGE=$(resolve python:3.12-slim-bookworm)
python3 - <<'PY'
import os, secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, quote
root=Path('deploy'); env=os.environ

def write_env(path, values, quoted=True):
    for key,value in values.items():
        if '\n' in value or '\r' in value or "'" in value:
            raise SystemExit(f'{key}: newline and single quote are not allowed in env values')
    path.write_text(''.join((f"{k}='{v}'\n" if quoted else f"{k}={v}\n") for k,v in values.items()))
    path.chmod(0o600)

passwords={name:secrets.token_hex(32) for name in ('postgres','app','worker','billing')}
(root/'secrets/postgres_password').write_text(passwords['postgres'])
(root/'secrets/postgres_password').chmod(0o600)
if env['HVM_DB_MODE']=='managed':
    admin=env.get('ADMIN_DATABASE_URL','')
    if not admin: raise SystemExit('For managed DB export ADMIN_DATABASE_URL with sslmode=verify-full before bootstrap.')
    parsed=urlsplit(admin)
    if 'sslmode=verify-full' not in parsed.query:
        raise SystemExit('Managed database DSN must enforce sslmode=verify-full.')
    host=parsed.hostname or ''
    port=f':{parsed.port}' if parsed.port else ''
    def dsn(role):
        return urlunsplit((parsed.scheme,f'hivemind_{role}:{passwords[role]}@{host}{port}',parsed.path,parsed.query,''))
else:
    admin=f"postgresql://postgres:{passwords['postgres']}@postgres:5432/hivemind"
    def dsn(role): return f"postgresql://hivemind_{role}:{passwords[role]}@postgres:5432/hivemind"
base=f"https://{env['HVM_DOMAIN']}"
write_env(root/'versions.env', {'POSTGRES_IMAGE':env['HVM_POSTGRES_IMAGE'],'CADDY_IMAGE':env['HVM_CADDY_IMAGE'],'PYTHON_IMAGE':env['HVM_PYTHON_IMAGE']})
write_env(root/'host.env', {'DOMAIN':env['HVM_DOMAIN'],'ACME_EMAIL':env['HVM_ACME_EMAIL'],'DB_MODE':env['HVM_DB_MODE'],'NETWORK_SUBNET':'172.28.172.0/24','CADDY_IP':'172.28.172.10'})
write_env(root/'state.env', {'APP_IMAGE_BLUE':env['HVM_INITIAL_IMAGE'],'APP_IMAGE_GREEN':env['HVM_INITIAL_IMAGE'],'WORKER_IMAGE':env['HVM_INITIAL_IMAGE'],'ACTIVE_COLOR':'none'})
write_env(root/'runtime.env', {'DATABASE_URL':dsn('app'),'BILLING_DATABASE_URL':dsn('billing'),'OPENAI_API_KEY':env.get('OPENAI_API_KEY',''),'ALLOWED_HOSTS':f"{env['HVM_DOMAIN']},localhost,127.0.0.1",'ALLOWED_ORIGINS':base,'PUBLIC_BASE_URL':base,'PUBLIC_ORIGIN':base,'HVM_PUBLIC_ORIGIN':base,'ENABLE_LEGACY_SSE':'false','AUTH_MODE':'api_key','FORWARDED_ALLOW_IPS':'172.28.172.10','SENTRY_DSN':env.get('SENTRY_DSN','')})
write_env(root/'worker.env', {'WORKER_DATABASE_URL':dsn('worker'),'OPENAI_API_KEY':env.get('OPENAI_API_KEY',''),'SENTRY_DSN':env.get('SENTRY_DSN',''),'EXTRACTION_MODEL':env.get('EXTRACTION_MODEL','gpt-4.1-mini-2025-04-14'),'LEDGER_WORKER_CONCURRENCY':env.get('LEDGER_WORKER_CONCURRENCY','2')})
write_env(root/'admin.env', {'PGDATABASE':admin,'HVM_APP_PASSWORD':passwords['app'],'HVM_WORKER_PASSWORD':passwords['worker'],'HVM_BILLING_PASSWORD':passwords['billing']}, quoted=False)
PY
cp Caddyfile deploy/caddy/Caddyfile
printf 'to blue:8000\n' > deploy/caddy/upstream.conf
if [[ "$HVM_DB_MODE" == selfhost ]]; then
  scripts/compose.sh --profile selfhost-db up -d --wait postgres
else
  scripts/compose.sh create --no-deps blue
fi
scripts/migrate.sh --initialize
printf '%s\n' 'Initialized. Set real billing/OAuth credentials in deploy/runtime.env, then run scripts/deploy.sh with the same immutable image digest.'
