#!/usr/bin/env python3
"""Migrate a fresh, isolated native proof database, then execute the proof harness.

This runner is only used by docker-compose.proof.yml. Its administrator credential
is a disposable test credential; the service exercised by the harness receives a
different, actual hivemind_app login. No provider or production secret is required.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import psycopg
import redis
from psycopg import sql


def main() -> int:
    if os.geteuid() == 0:
        raise RuntimeError("The native proof runner must execute as a non-root user")
    root = Path(__file__).resolve().parents[1]
    admin_password = os.environ["HVM_PROOF_ADMIN_PASSWORD"]
    app_password = os.environ["HVM_PROOF_APP_PASSWORD"]
    admin_url = (
        "postgresql://postgres:" + quote(admin_password, safe="")
        + "@postgres:5432/hivemind_proof?sslmode=disable"
    )
    app_url = (
        "postgresql://hivemind_app:" + quote(app_password, safe="")
        + "@postgres:5432/hivemind_proof?sslmode=disable"
    )
    with redis.Redis(host="redis", socket_timeout=3) as cache:
        if not cache.ping():
            raise RuntimeError("Redis readiness failed")
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS public.hivemind_schema_migrations (
                version text PRIMARY KEY,
                sha256 char(64) NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        connection.execute("REVOKE ALL ON public.hivemind_schema_migrations FROM PUBLIC")
        for path in sorted((root / "sql").glob("[0-9][0-9][0-9]_*.sql")):
            source = path.read_bytes()
            digest = hashlib.sha256(source).hexdigest()
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (7107331,))
                old = connection.execute(
                    "SELECT sha256 FROM public.hivemind_schema_migrations WHERE version = %s",
                    (path.name,),
                ).fetchone()
                if old:
                    if old[0] != digest:
                        raise RuntimeError("An applied migration's digest changed: " + path.name)
                    continue
                connection.execute(source.decode("utf-8"), prepare=False)
                connection.execute("RESET ROLE")
                connection.execute(
                    "INSERT INTO public.hivemind_schema_migrations (version, sha256) VALUES (%s, %s)",
                    (path.name, digest),
                )
            print("PASS migration " + path.name, flush=True)
        # PostgreSQL ALTER ROLE does not accept bind parameters for PASSWORD.
        # psycopg's composable Literal quotes the value; it is never interpolated.
        connection.execute(
            sql.SQL("ALTER ROLE hivemind_app PASSWORD {}").format(sql.Literal(app_password))
        )
    child_env = os.environ.copy()
    child_env.update({
        "INVESTOR_ADMIN_DATABASE_URL": admin_url,
        "INVESTOR_APP_DATABASE_URL": app_url,
        "TEST_DATABASE_URL": admin_url,
        "PYTHONPATH": str(root / "src"),
    })
    commands = [
        # Concurrency fixtures require an empty queue; the later investor workflow
        # intentionally retains its immutable events and queued extraction jobs.
        [sys.executable, "-m", "pytest", "tests/test_native_concurrency.py", "-q", "--tb=short"],
        [sys.executable, "tests/investor_proof_harness.py", "--output", "/evidence/investor-proof.json"],
        [sys.executable, "tests/investor_proof_harness.py", "--verify", "/evidence/investor-proof.json"],
        [sys.executable, "tests/verify_ledger_model.py", "--output", "/evidence/ledger-model-proof.json"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=root, env=child_env, check=False)  # noqa: S603 -- fixed local commands
        if result.returncode != 0:
            return result.returncode
    print("PASS native proof, receipt verification, bounded state model, and concurrency tests", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("FAIL proof runner setup. No credentials or database payloads are printed.", file=sys.stderr)
        raise SystemExit(1) from None
