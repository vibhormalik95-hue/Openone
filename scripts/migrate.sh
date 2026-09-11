#!/usr/bin/env bash
# Only deployment/admin uses this connection. No API container receives it.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
source deploy/versions.env
[[ -r deploy/admin.env ]] || { echo 'Missing protected deploy/admin.env' >&2; exit 1; }
psql_admin() {
  docker run --rm -i --network hivemind_backend --env-file deploy/admin.env \
    "$POSTGRES_IMAGE" psql -X -q --set ON_ERROR_STOP=1 "$@"
}
psql_admin <<'SQL'
CREATE TABLE IF NOT EXISTS public.hivemind_schema_migrations (
  version text PRIMARY KEY,
  sha256 char(64) NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON public.hivemind_schema_migrations FROM PUBLIC;
SQL
for file in sql/[0-9][0-9][0-9]_*.sql; do
  name=$(basename "$file")
  hash=$(sha256sum "$file" | cut -d ' ' -f 1)
  applied=$(psql_admin -tA -c "SELECT sha256 FROM public.hivemind_schema_migrations WHERE version='$name'")
  if [[ -n "$applied" ]]; then
    [[ "$applied" == "$hash" ]] || { echo "Applied migration changed: $name" >&2; exit 1; }
    continue
  fi
  # Files contain no top-level BEGIN/COMMIT: DDL and ledger record commit together.
  {
    printf "SET lock_timeout = '5s'; SELECT pg_advisory_xact_lock(7107331);\n"
    cat "$file"
    printf "\nRESET ROLE; INSERT INTO public.hivemind_schema_migrations(version,sha256) VALUES ('%s','%s');\n" "$name" "$hash"
  } | psql_admin --single-transaction
  printf 'Applied %s\n' "$name"
done
if [[ ${1:-} == --initialize ]]; then
  psql_admin --single-transaction < deploy/set-role-passwords.sql
fi
