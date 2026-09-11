"""Integration tests: run after migrations on disposable PostgreSQL 17 + pgvector.

TEST_DATABASE_URL (or ADMIN_DATABASE_URL) must name a test database and a role
allowed to SET ROLE. Fixtures rollback all changes. No production DSN defaults.
"""
from __future__ import annotations

import hashlib
import os
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

DSN = os.getenv("TEST_DATABASE_URL") or os.getenv("ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL for PostgreSQL integration tests")
VECTOR = "[" + ",".join(["1"] + ["0"] * 1535) + "]"


def payload(*, memories=None, constraints=None):
    return {
        "source": {"client": "pytest", "conversation_id": "database-test"},
        "memories": memories if memories is not None else [{"kind": "decision", "text": "Use PostgreSQL 17", "metadata": {}}],
        "constraints": constraints if constraints is not None else [],
    }


def constraint(key="database", value="PostgreSQL 17", version=0, status="active"):
    return {"key": key, "value": value, "expected_version": version, "status": status}


def commit(conn, project_id, body=None, idempotency=None):
    return conn.execute(
        "SELECT public.commit_project_memory(%s,%s,%s)",
        (project_id, idempotency or str(uuid.uuid4()), Jsonb(body or payload())),
    ).fetchone()[0]


def authenticate(conn, key_hash):
    conn.execute("SET LOCAL ROLE hivemind_app")
    return conn.execute("SELECT * FROM public.authenticate_api_key(%s)", (key_hash,)).fetchone()


@pytest.fixture
def db():
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.transaction(force_rollback=True):
            tenant_a, tenant_b, project_a, project_a_private, project_b, key_a, key_b = [uuid.uuid4() for _ in range(7)]
            hash_a = hashlib.sha256(os.urandom(32)).hexdigest()
            hash_b = hashlib.sha256(os.urandom(32)).hexdigest()
            conn.execute("INSERT INTO tenants(id,name,billing_status) VALUES(%s,'Tenant A','active'),(%s,'Tenant B','active')", (tenant_a, tenant_b))
            conn.execute("INSERT INTO projects(id,tenant_id,name,slug) VALUES(%s,%s,'A','a'),(%s,%s,'Private','private'),(%s,%s,'B','b')", (project_a, tenant_a, project_a_private, tenant_a, project_b, tenant_b))
            conn.execute("INSERT INTO api_keys(id,tenant_id,key_hash,prefix) VALUES(%s,%s,%s,'hvm_test_a'),(%s,%s,%s,'hvm_test_b')", (key_a, tenant_a, hash_a, key_b, tenant_b, hash_b))
            conn.execute("INSERT INTO api_key_projects(tenant_id,api_key_id,project_id) VALUES(%s,%s,%s),(%s,%s,%s)", (tenant_a, key_a, project_a, tenant_b, key_b, project_b))
            yield conn, {
                "tenant_a": tenant_a, "tenant_b": tenant_b,
                "project_a": project_a, "private": project_a_private, "project_b": project_b,
                "key_a": key_a, "key_b": key_b, "hash_a": hash_a, "hash_b": hash_b,
            }


def test_default_deny_and_same_tenant_project_scope(db):
    conn, ids = db
    conn.execute("SET LOCAL ROLE hivemind_app")
    assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
    authenticate(conn, ids["hash_a"])
    assert conn.execute("SELECT id FROM projects").fetchall() == [(ids["project_a"],)]
    assert conn.execute("SELECT can_access_project(%s,false)", (ids["private"],)).fetchone()[0] is False
    assert conn.execute("SELECT can_access_project(%s,false)", (ids["project_b"],)).fetchone()[0] is False


def test_known_key_uuid_is_not_authentication_proof(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    conn.execute("SELECT set_config('app.api_key_id',%s,true)", (str(ids["key_b"]),))
    assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 0


def test_cross_tenant_write_rejected(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        commit(conn, ids["project_b"])


def test_api_key_hashes_are_not_readable(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("SELECT key_hash FROM api_keys")


def test_revocation_rechecked_inside_transaction(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    conn.execute("RESET ROLE")
    conn.execute("UPDATE api_keys SET revoked_at=now() WHERE id=%s", (ids["key_a"],))
    conn.execute("SET LOCAL ROLE hivemind_app")
    assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
    with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification), conn.transaction():
        authenticate(conn, ids["hash_a"])


def test_idempotency_replay_and_changed_payload_conflict(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    request_id = str(uuid.uuid4())
    first = commit(conn, ids["project_a"], idempotency=request_id)
    replay = commit(conn, ids["project_a"], idempotency=request_id)
    assert conn.execute("SELECT actor_key_id FROM conversation_events").fetchone()[0] == ids["key_a"]
    assert first["event_id"] == replay["event_id"]
    assert replay["replayed"] is True
    assert conn.execute("SELECT count(*) FROM conversation_events").fetchone()[0] == 1
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        commit(conn, ids["project_a"], payload(memories=[{"kind": "note", "text": "Changed"}]), request_id)
    assert conn.execute("SELECT value FROM usage_counters WHERE metric='commits'").fetchone()[0] == 1


def test_exact_content_dedup_retains_both_source_events(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    assert commit(conn, ids["project_a"])["memories_inserted"] == 1
    assert commit(conn, ids["project_a"])["memories_inserted"] == 0
    assert conn.execute("SELECT count(*) FROM conversation_events").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM embedding_jobs").fetchone()[0] == 1


def test_constraint_version_conflict_rolls_back_whole_commit(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    commit(conn, ids["project_a"], payload(memories=[], constraints=[constraint()]))
    with pytest.raises(psycopg.errors.SerializationFailure), conn.transaction():
        commit(conn, ids["project_a"], payload(constraints=[constraint(value="Other DB", version=0)]))
    assert conn.execute("SELECT count(*) FROM conversation_events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0] == 0
    commit(conn, ids["project_a"], payload(memories=[], constraints=[constraint(value="PostgreSQL 18", version=1)]))
    assert conn.execute("SELECT value,version FROM constraints_ledger WHERE active").fetchall() == [("PostgreSQL 18", 2)]
    assert conn.execute("SELECT count(*) FROM constraints_ledger").fetchone()[0] == 2


def test_recall_preserves_all_exact_constraints_without_provider(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    commit(conn, ids["project_a"], payload(constraints=[constraint(), constraint("region", "Canada")]))
    packet = conn.execute("SELECT match_project_context(%s,NULL,1)", (ids["project_a"],)).fetchone()[0]
    assert {item["key"] for item in packet["constraints"]} == {"database", "region"}
    assert packet["constraints_complete"] is True
    assert packet["memories"] == []
    assert packet["semantic_index_pending"] == 1


def test_constraint_budget_errors_instead_of_silent_truncation(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    commit(conn, ids["project_a"], payload(memories=[], constraints=[constraint(f"key_{i}", i) for i in range(20)]))
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        commit(conn, ids["project_a"], payload(memories=[], constraints=[constraint(f"key_{i}", i) for i in range(20, 33)]))
    assert conn.execute("SELECT count(*) FROM constraints_ledger WHERE active").fetchone()[0] == 20


def test_composite_foreign_key_blocks_mismatched_tenant(db):
    conn, ids = db
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("INSERT INTO api_key_projects(tenant_id,api_key_id,project_id) VALUES(%s,%s,%s)", (ids["tenant_a"], ids["key_a"], ids["project_b"]))


def test_starter_cap_enforced_by_database(db):
    conn, ids = db
    conn.execute("SET LOCAL ROLE hivemind_billing")
    for i in range(3):
        conn.execute("INSERT INTO projects(tenant_id,name,slug) VALUES(%s,%s,%s)", (ids["tenant_a"], f"Extra {i}", f"extra-{i}"))
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute("INSERT INTO projects(tenant_id,name,slug) VALUES(%s,'Sixth','sixth')", (ids["tenant_a"],))
    conn.execute("UPDATE tenants SET plan='pro' WHERE id=%s", (ids["tenant_a"],))
    conn.execute("INSERT INTO projects(tenant_id,name,slug) VALUES(%s,'Sixth','sixth')", (ids["tenant_a"],))


def test_worker_lease_fencing_and_hybrid_recall(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    commit(conn, ids["project_a"], payload(constraints=[constraint()]))
    conn.execute("RESET ROLE")
    conn.execute("SET LOCAL ROLE hivemind_worker")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("SELECT content FROM memory_embeddings")
    job_id, lease_token, memory_id, content = conn.execute("SELECT * FROM claim_embedding_jobs(1)").fetchone()
    assert content == "Use PostgreSQL 17"
    assert conn.execute("SELECT * FROM claim_embedding_jobs(1)").fetchall() == []
    assert conn.execute("SELECT finish_embedding_job(%s,%s,%s::vector,%s)", (job_id, uuid.uuid4(), VECTOR, "text-embedding-3-small")).fetchone()[0] is False
    assert conn.execute("SELECT finish_embedding_job(%s,%s,%s::vector,%s)", (job_id, lease_token, VECTOR, "text-embedding-3-small")).fetchone()[0] is True
    assert conn.execute("SELECT finish_embedding_job(%s,%s,%s::vector,%s)", (job_id, lease_token, VECTOR, "text-embedding-3-small")).fetchone()[0] is False
    conn.execute("RESET ROLE")
    authenticate(conn, ids["hash_a"])
    packet = conn.execute("SELECT match_project_context(%s,%s::vector,1)", (ids["project_a"], VECTOR)).fetchone()[0]
    assert packet["memories"][0]["memory_id"] == str(memory_id)
    assert packet["memories"][0]["similarity"] == pytest.approx(1)
    assert len(packet["constraints"]) == 1
    assert packet["semantic_index_pending"] == 0


def test_runtime_and_helper_roles_cannot_bypass_rls(db):
    conn, _ = db
    rows = conn.execute("SELECT rolname,rolsuper,rolbypassrls FROM pg_roles WHERE rolname LIKE 'hivemind_%'").fetchall()
    assert rows and all(not superuser and not bypass for _, superuser, bypass in rows)
    tables = conn.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid IN ('projects'::regclass,'memory_embeddings'::regclass,'api_keys'::regclass)").fetchall()
    assert tables == [(True, True)] * 3


def test_transaction_local_auth_does_not_leak_after_rollback(db):
    conn, ids = db
    conn.execute("SET LOCAL ROLE hivemind_app")
    with conn.transaction(force_rollback=True):
        authenticate(conn, ids["hash_a"])
        assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 0


def test_recall_quota_precheck_blocks_provider_spend(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    conn.execute("SELECT ensure_recall_allowance()")
    conn.execute(
        "INSERT INTO usage_counters(tenant_id,period_start,metric,value) VALUES(%s,date_trunc('month',now() AT TIME ZONE 'UTC')::date,'recalls',25000)",
        (ids["tenant_a"],),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="monthly_usage_limit"), conn.transaction():
        conn.execute("SELECT ensure_recall_allowance()")
    assert conn.execute("SELECT value FROM usage_counters WHERE metric='recalls'").fetchone()[0] == 25000


def test_same_tenant_actor_spoof_rejected(db):
    conn, ids = db
    other_key = uuid.uuid4()
    conn.execute(
        "INSERT INTO api_keys(id,tenant_id,key_hash,prefix) VALUES(%s,%s,%s,'hvm_other')",
        (other_key, ids["tenant_a"], hashlib.sha256(os.urandom(32)).hexdigest()),
    )
    authenticate(conn, ids["hash_a"])
    commit(conn, ids["project_a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute(
            """INSERT INTO conversation_events(tenant_id,project_id,actor_key_id,idempotency_key,payload_hash,payload,source)
               SELECT tenant_id,project_id,%s,%s,payload_hash,payload,source FROM conversation_events LIMIT 1""",
            (other_key, str(uuid.uuid4())),
        )



def test_fresh_recall_can_reactivate_superseded_constraint(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    commit(conn, ids["project_a"], payload(memories=[], constraints=[constraint()]))
    commit(conn, ids["project_a"], payload(memories=[], constraints=[constraint(version=1, status="superseded")]))
    packet = conn.execute("SELECT match_project_context(%s,NULL,1)", (ids["project_a"],)).fetchone()[0]
    assert packet["constraints"] == []
    assert packet["constraint_versions"] == {"database": 2}
    commit(conn, ids["project_a"], payload(memories=[], constraints=[constraint(value="PostgreSQL 18", version=packet["constraint_versions"]["database"])]))
    updated = conn.execute("SELECT match_project_context(%s,NULL,1)", (ids["project_a"],)).fetchone()[0]
    assert updated["constraint_versions"] == {"database": 3}
    assert updated["constraints"][0]["value"] == "PostgreSQL 18"


def test_distinct_historical_constraint_key_budget_is_atomic(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    for start in range(0, 128, 20):
        commit(conn, ids["project_a"], payload(memories=[], constraints=[
            constraint(f"historical_{i}", i, status="superseded")
            for i in range(start, min(start + 20, 128))
        ]))
    with pytest.raises(psycopg.errors.CheckViolation, match="constraint_key_budget_exceeded"), conn.transaction():
        commit(conn, ids["project_a"], payload(memories=[], constraints=[constraint("too_many", 129)]))
    packet = conn.execute("SELECT match_project_context(%s,NULL,1)", (ids["project_a"],)).fetchone()[0]
    assert len(packet["constraint_versions"]) == 128
    assert "too_many" not in packet["constraint_versions"]
    assert packet["constraints"] == []
