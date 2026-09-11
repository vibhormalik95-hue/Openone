#!/usr/bin/env bash
# Restore to a NEW database on configured PostgreSQL cluster, never the live DB.
# scripts/restore.sh backups/TIMESTAMP.dump.age hivemind_restore_YYYYMMDD /secure/age-identity.txt
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")/.."
[[ $# == 3 ]] || { echo 'Usage: restore.sh ENCRYPTED_DUMP NEW_RESTORE_DATABASE AGE_IDENTITY_FILE' >&2; exit 2; }
backup="$1"; database="$2"; identity="$3"
[[ "$database" =~ ^hivemind_restore_[a-z0-9_]{1,35}$ ]] || { echo 'Target must be a new hivemind_restore_* database.' >&2; exit 2; }
[[ -r "$backup" && -r "$identity" ]] || exit 2
source deploy/versions.env
restore_env=$(mktemp deploy/restore.XXXXXX.env)
trap 'rm -f "$restore_env"' EXIT
export HVM_RESTORE_DATABASE="$database" HVM_RESTORE_ENV="$restore_env"
python3 - <<'PY'
import os
from pathlib import Path
from urllib.parse import urlsplit, unquote, parse_qsl
source=dict(line.split('=',1) for line in Path('deploy/admin.env').read_text().splitlines() if '=' in line)
p=urlsplit(source['PGDATABASE']); values={'PGHOST':p.hostname or '', 'PGPORT':str(p.port or 5432), 'PGUSER':unquote(p.username or ''), 'PGPASSWORD':unquote(p.password or ''), 'PGDATABASE':os.environ['HVM_RESTORE_DATABASE']}
allowed={'sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY','channel_binding':'PGCHANNELBINDING','options':'PGOPTIONS'}
for key,value in parse_qsl(p.query):
    if key in allowed: values[allowed[key]]=value
Path(os.environ['HVM_RESTORE_ENV']).write_text(''.join(f'{k}={v}\n' for k,v in values.items()))
PY
docker run --rm --network hivemind_backend --env-file deploy/admin.env "$POSTGRES_IMAGE" \
  psql -X -v ON_ERROR_STOP=1 -c "CREATE DATABASE $database"
age --decrypt --identity "$identity" "$backup" |
  docker run --rm -i --network hivemind_backend --env-file "$restore_env" "$POSTGRES_IMAGE" \
    pg_restore --exit-on-error --dbname "$database"
docker run --rm -i --network hivemind_backend --env-file "$restore_env" "$POSTGRES_IMAGE" \
  psql -X -v ON_ERROR_STOP=1 <<'SQL'
SELECT version();
SELECT extversion FROM pg_extension WHERE extname='vector';
SELECT 'tenants' AS table_name, count(*) FROM tenants
UNION ALL SELECT 'projects', count(*) FROM projects
UNION ALL SELECT 'conversation_events', count(*) FROM conversation_events;
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_class WHERE relname IN ('projects','conversation_events','memory_embeddings') AND (NOT relrowsecurity OR NOT relforcerowsecurity)) THEN
    RAISE EXCEPTION 'Restored tenant tables are missing FORCE RLS';
  END IF;
  IF EXISTS (SELECT FROM pg_roles WHERE rolname IN ('hivemind_app','hivemind_worker','hivemind_billing') AND (rolsuper OR rolbypassrls)) THEN
    RAISE EXCEPTION 'Restored runtime roles are unsafe';
  END IF;
END $$;
ANALYZE;
SQL
printf 'Restored and checked %s. Run tenant-isolation and application smoke tests against this disposable DB before cutover.\n' "$database"
