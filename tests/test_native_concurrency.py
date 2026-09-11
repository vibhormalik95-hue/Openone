"""Native PostgreSQL acceptance gates; never run these against PGlite.

Export TEST_DATABASE_URL for a dedicated, disposable database after all six
migrations, then run:
HVM_NATIVE_CONCURRENCY=1 pytest tests/test_native_concurrency.py -q

These tests require committed fixtures so independent server sessions can see
them. Immutable event/ledger rows remain afterward; discard the test database.
Only each fixture's own queued jobs are canceled during teardown.
"""
from __future__ import annotations

import hashlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.rows import dict_row
from test_database import VECTOR
from test_unified_database import claim, envelope, sync

DSN = os.getenv("TEST_DATABASE_URL")
ENABLED = os.getenv("HVM_NATIVE_CONCURRENCY") == "1"
pytestmark = pytest.mark.skipif(
    not (DSN and ENABLED),
    reason="requires native PostgreSQL, dedicated TEST_DATABASE_URL and HVM_NATIVE_CONCURRENCY=1",
)


def parallel(first, second):
    """Release two separately connected SQL sessions at the same barrier."""
    barrier = threading.Barrier(2, timeout=10)

    def invoke(action):
        with psycopg.connect(DSN, autocommit=True, connect_timeout=5) as connection:
            with connection.transaction():
                connection.execute("SET LOCAL statement_timeout = '15s'")
                barrier.wait()
                return action(connection)

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(invoke, first), executor.submit(invoke, second)]
        return [job.result(timeout=25) for job in jobs]


@pytest.fixture
def native_project():
    with psycopg.connect(DSN, autocommit=True, connect_timeout=5) as admin:
        version = admin.execute("SELECT version()").fetchone()[0]
        if any(marker in version.lower() for marker in ("emscripten", "pglite", "wasm")):
            pytest.skip("PGlite has one shared session; native concurrency cannot be validated")
        assert admin.execute("SHOW server_version_num").fetchone()[0].startswith("17")
        assert admin.execute(
            "SELECT count(*) FROM ledger_outbox WHERE status IN ('queued','running')"
        ).fetchone()[0] == 0, "Use an isolated disposable DB with no active background work"
        tenant, project, key = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        key_hash = hashlib.sha256(os.urandom(32)).hexdigest()
        with admin.transaction():
            admin.execute(
                "INSERT INTO tenants(id,name,billing_status) VALUES(%s,'native-concurrency-fixture','active')",
                (tenant,),
            )
            admin.execute(
                "INSERT INTO projects(id,tenant_id,name,slug) VALUES(%s,%s,'Race fixture','race')",
                (project, tenant),
            )
            admin.execute(
                "INSERT INTO api_keys(id,tenant_id,key_hash,prefix) VALUES(%s,%s,%s,'hvm_race')",
                (key, tenant, key_hash),
            )
            admin.execute(
                "INSERT INTO api_key_projects(tenant_id,api_key_id,project_id) VALUES(%s,%s,%s)",
                (tenant, key, project),
            )
        try:
            yield admin, project, key_hash
        finally:
            admin.execute(
                "UPDATE ledger_outbox SET status='canceled',lease_token=NULL,lease_until=NULL,"
                "last_error='test_fixture_complete',completed_at=clock_timestamp() "
                "WHERE project_id=%s AND status IN ('queued','running')",
                (project,),
            )


def app(connection, key_hash):
    connection.execute("SET LOCAL ROLE hivemind_app")
    connection.execute("SELECT * FROM authenticate_api_key(%s)", (key_hash,))


def test_native_competing_assertions_have_one_winner(native_project):
    admin, project, key_hash = native_project

    def submit(value):
        def operation(connection):
            app(connection, key_hash)
            try:
                with connection.transaction():
                    return sync(connection, project, envelope([claim(value=value)]))
            except psycopg.errors.SerializationFailure as error:
                return error.sqlstate
        return operation

    outcomes = parallel(submit("PostgreSQL 17"), submit("PostgreSQL 18"))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert outcomes.count("40001") == 1
    assert admin.execute(
        "SELECT count(*) FROM ledger_constraints WHERE project_id=%s", (project,)
    ).fetchone()[0] == 1
    assert admin.execute(
        "SELECT count(*) FROM memory_events WHERE project_id=%s", (project,)
    ).fetchone()[0] == 1


def test_native_same_idempotency_key_has_one_event(native_project):
    admin, project, key_hash = native_project
    body = envelope([claim()])

    def submit(connection):
        app(connection, key_hash)
        return sync(connection, project, body)

    first, second = parallel(submit, submit)
    assert first["commit"]["event_id"] == second["commit"]["event_id"]
    assert sorted([first["commit"]["replayed"], second["commit"]["replayed"]]) == [False, True]
    assert admin.execute(
        "SELECT count(*) FROM memory_events WHERE project_id=%s", (project,)
    ).fetchone()[0] == 1


def test_native_workers_skip_locked_and_fence_reclaimed_lease(native_project):
    admin, project, key_hash = native_project
    with admin.transaction():
        app(admin, key_hash)
        sync(admin, project, envelope([claim()]))

    def acquire(connection):
        connection.execute("SET LOCAL ROLE hivemind_worker")
        with connection.cursor(row_factory=dict_row) as cursor:
            return cursor.execute("SELECT * FROM claim_ledger_jobs(1)").fetchall()

    claimed = parallel(acquire, acquire)
    assert sorted(len(batch) for batch in claimed) == [0, 1]
    original = next(batch[0] for batch in claimed if batch)
    admin.execute(
        "UPDATE ledger_outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
        (original["job_id"],),
    )
    with admin.transaction():
        fresh = acquire(admin)[0]
    assert fresh["job_id"] == original["job_id"]
    assert fresh["lease_token"] != original["lease_token"]

    def finish(lease):
        def operation(connection):
            connection.execute("SET LOCAL ROLE hivemind_worker")
            return connection.execute(
                "SELECT finish_ledger_embedding_job(%s,%s,%s::vector,%s)",
                (original["job_id"], lease, VECTOR, "text-embedding-3-small"),
            ).fetchone()[0]
        return operation

    assert parallel(finish(original["lease_token"]), finish(fresh["lease_token"])) == [False, True]
    assert admin.execute(
        "SELECT status FROM ledger_outbox WHERE id=%s", (original["job_id"],)
    ).fetchone()[0] == "done"
