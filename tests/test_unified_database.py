"""Regression coverage for additive migration 005 on disposable PostgreSQL 17.

Run with TEST_DATABASE_URL against an already migrated administrator-owned test DB.
The imported fixture rolls every test back, including generated work and API keys.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb
from test_database import DSN, VECTOR, authenticate
from test_database import db as database_fixture

db = database_fixture

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL for PostgreSQL integration tests")


def claim(entity="database", value="PostgreSQL 17", version=0, state="accepted", kind="constraint"):
    return {
        "entity_key": entity,
        "kind": kind,
        "state": state,
        "value": value,
        "expected_version": version,
        "evidence": "The user explicitly selected this database.",
    }


def envelope(claims=None, fragment=None, request_id=None):
    return {
        "idempotency_key": str(request_id or uuid.uuid4()),
        "source": {"client": "pytest", "conversation_id": "unified-ledger-test"},
        "fragment": fragment,
        "claims": claims if claims is not None else [claim()],
    }


def sync(conn, project, body=None, query="PostgreSQL", vector=None):
    return conn.execute(
        "SELECT public.execute_autonomous_sync(%s,%s,%s::vector,%s)",
        (project, query, vector, Jsonb(body) if body is not None else None),
    ).fetchone()[0]


def worker(conn):
    conn.execute("RESET ROLE")
    conn.execute("SET LOCAL ROLE hivemind_worker")


def test_unified_project_resolution_is_token_scoped(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    assert conn.execute("SELECT resolve_authorized_project(NULL)").fetchone()[0] == ids["project_a"]
    assert sync(conn, None)["project_id"] == str(ids["project_a"])
    for other in (ids["private"], ids["project_b"]):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            sync(conn, other)
    conn.execute("RESET ROLE")
    conn.execute(
        "INSERT INTO api_key_projects(tenant_id,api_key_id,project_id) VALUES(%s,%s,%s)",
        (ids["tenant_a"], ids["key_a"], ids["private"]),
    )
    authenticate(conn, ids["hash_a"])
    with pytest.raises(psycopg.errors.InvalidParameterValue, match="project_required"), conn.transaction():
        sync(conn, None)


def test_unified_runtime_cannot_forge_rows_or_worker_scope(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, ids["project_a"], envelope())
    for relation in ("immutable_event_log", "authoritative_constraints", "semantic_embeddings", "ledger_outbox"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(psycopg.sql.SQL("INSERT INTO {} DEFAULT VALUES").format(psycopg.sql.Identifier(relation)))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("SELECT * FROM claim_ledger_jobs(1)")
    conn.execute("SELECT set_config('app.tenant_id',%s,true)", (str(ids["tenant_b"]),))
    conn.execute("SELECT set_config('app.api_key_id',%s,true)", (str(ids["key_b"]),))
    assert conn.execute("SELECT count(*) FROM immutable_event_log").fetchone()[0] == 0


def test_tentative_proposal_does_not_replace_authoritative_head(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, None, envelope())
    packet = sync(conn, None, envelope([claim(value="Consider SQLite", version=1, state="tentative")]))
    assert packet["ledger_versions"] == {"database": 2}
    assert packet["constraints"][0]["value"] == "PostgreSQL 17"
    assert packet["constraints"][0]["version"] == 1
    assert packet["constraints_complete"] is True
    promoted = sync(conn, None, envelope([claim(value="SQLite", version=2)]))
    assert promoted["constraints"][0]["value"] == "SQLite"
    assert promoted["ledger_versions"] == {"database": 3}


def test_retraction_and_later_tentative_stay_retracted(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, None, envelope())
    removed = sync(conn, None, envelope([claim(version=1, state="retracted")]))
    assert removed["constraints"] == []
    proposed = sync(conn, None, envelope([claim(value="SQLite", version=2, state="tentative")]))
    assert proposed["constraints"] == []
    assert proposed["ledger_versions"] == {"database": 3}
    accepted = sync(conn, None, envelope([claim(value="SQLite", version=3)]))
    assert accepted["constraints"][0]["version"] == 4


def test_duplicate_current_authority_precedes_stale_version_check(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, None, envelope())
    sync(conn, None, envelope([claim(value="SQLite proposal", version=1, state="tentative")]))
    repeated = sync(conn, None, envelope())
    assert repeated["commit"]["claims_inserted"] == 0
    assert repeated["commit"]["claims_deduplicated"] == 1
    assert repeated["ledger_versions"] == {"database": 2}
    with pytest.raises(psycopg.errors.SerializationFailure), conn.transaction():
        sync(conn, None, envelope([claim(value="MySQL", version=0)]))
    assert conn.execute("SELECT count(*) FROM immutable_event_log").fetchone()[0] == 3


def test_old_historical_value_is_a_versioned_revert(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, None, envelope())
    sync(conn, None, envelope([claim(value="SQLite", version=1)]))
    with pytest.raises(psycopg.errors.SerializationFailure), conn.transaction():
        sync(conn, None, envelope())
    reverted = sync(conn, None, envelope([claim(version=2)]))
    assert reverted["constraints"][0]["version"] == 3
    assert reverted["commit"]["claims_inserted"] == 1


def test_unified_idempotency_conflict_rolls_back_everything(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    body = envelope(fragment="The user chose PostgreSQL 17.")
    first = sync(conn, None, body)
    replay = sync(conn, None, body)
    assert first["commit"]["event_id"] == replay["commit"]["event_id"]
    assert replay["commit"]["replayed"] is True
    assert conn.execute("SELECT count(*) FROM immutable_event_log").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM ledger_outbox").fetchone()[0] == 3
    changed = dict(body, fragment="Different content.")
    with pytest.raises(psycopg.errors.UniqueViolation, match="idempotency_conflict"), conn.transaction():
        sync(conn, None, changed)
    assert conn.execute("SELECT count(*) FROM immutable_event_log").fetchone()[0] == 1
    assert conn.execute("SELECT value FROM usage_counters WHERE metric='commits'").fetchone()[0] == 1


def test_json_syntax_and_numeric_scale_deduplicate(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    first = claim(value={"replicas": 1, "region": "Canada"})
    second = claim(entity=" DATABASE ", value={"region": "Canada", "replicas": 1.0})
    sync(conn, None, envelope([first]))
    response = sync(conn, None, envelope([second]))
    assert response["commit"]["claims_deduplicated"] == 1
    assert response["ledger_versions"] == {"database": 1}


def test_immutable_history_blocks_even_administrator_updates_and_delete(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, None, envelope())
    conn.execute("RESET ROLE")
    for statement in (
        "UPDATE authoritative_constraints SET state='retracted'",
        "DELETE FROM authoritative_constraints",
        "UPDATE immutable_event_log SET fragment='changed'",
        "DELETE FROM immutable_event_log",
        "TRUNCATE authoritative_constraints CASCADE",
    ):
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="immutable_history"), conn.transaction():
            conn.execute(statement)


def test_outbox_lease_fencing_and_extraction_cannot_promote(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, None, envelope(fragment="Perhaps SQLite would be simpler."))
    worker(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("SELECT content FROM semantic_embeddings")
    jobs = conn.execute("SELECT * FROM claim_ledger_jobs(10)").fetchall()
    assert len(jobs) == 3
    assert conn.execute("SELECT * FROM claim_ledger_jobs(10)").fetchall() == []
    job = next(j for j in jobs if j[2] == "extraction")
    with pytest.raises(psycopg.errors.InvalidParameterValue, match="tentative"), conn.transaction():
        conn.execute("SELECT finish_ledger_extraction_job(%s,%s,%s,%s)", (job[0], job[1], Jsonb([claim(value="SQLite")]), "test-extractor"))
    assert conn.execute(
        "SELECT finish_ledger_extraction_job(%s,%s,%s,%s)",
        (job[0], uuid.uuid4(), Jsonb([claim(value="SQLite", state="tentative")]), "test-extractor"),
    ).fetchone()[0] is False
    assert conn.execute(
        "SELECT finish_ledger_extraction_job(%s,%s,%s,%s)",
        (job[0], job[1], Jsonb([claim(value="SQLite", state="tentative")]), "test-extractor"),
    ).fetchone()[0] is True
    conn.execute("RESET ROLE")
    authenticate(conn, ids["hash_a"])
    response = sync(conn, None)
    assert response["constraints"][0]["value"] == "PostgreSQL 17"
    assert response["ledger_versions"] == {"database": 2}
    assert response["pending_extraction"] == 0


def test_worker_revocation_rechecked_on_claim_and_completion(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, None, envelope(fragment="Consider using SQLite."))
    worker(conn)
    job = conn.execute("SELECT * FROM claim_ledger_jobs(1)").fetchone()
    conn.execute("RESET ROLE")
    conn.execute("UPDATE api_keys SET revoked_at=clock_timestamp() WHERE id=%s", (ids["key_a"],))
    worker(conn)
    if job[2] == "embedding":
        succeeded = conn.execute("SELECT finish_ledger_embedding_job(%s,%s,%s::vector,%s)", (job[0], job[1], VECTOR, "text-embedding-3-small")).fetchone()[0]
    else:
        succeeded = conn.execute("SELECT finish_ledger_extraction_job(%s,%s,%s,%s)", (job[0], job[1], Jsonb([]), "test-extractor")).fetchone()[0]
    assert succeeded is False
    assert conn.execute("SELECT * FROM claim_ledger_jobs(10)").fetchall() == []
    conn.execute("RESET ROLE")
    assert conn.execute("SELECT DISTINCT status FROM ledger_outbox").fetchall() == [("canceled",)]


def test_retry_budget_and_expired_fence(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, None, envelope())
    worker(conn)
    first = conn.execute("SELECT * FROM claim_ledger_jobs(1)").fetchone()
    conn.execute("RESET ROLE")
    conn.execute("UPDATE ledger_outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (first[0],))
    worker(conn)
    second = conn.execute("SELECT * FROM claim_ledger_jobs(1)").fetchone()
    assert second[0] == first[0] and second[1] != first[1]
    assert conn.execute("SELECT fail_ledger_job(%s,%s,'provider_timeout')", (first[0], first[1])).fetchone()[0] is False
    assert conn.execute("SELECT fail_ledger_job(%s,%s,'provider_timeout')", (second[0], second[1])).fetchone()[0] is True
    conn.execute("RESET ROLE")
    conn.execute("UPDATE ledger_outbox SET attempts=5,available_at=clock_timestamp()-interval '1 second' WHERE id=%s", (first[0],))
    worker(conn)
    assert conn.execute("SELECT * FROM claim_ledger_jobs(1)").fetchall() == []
    conn.execute("RESET ROLE")
    assert conn.execute("SELECT status FROM ledger_outbox WHERE id=%s", (first[0],)).fetchone()[0] == "dead"


def test_hybrid_recall_preserves_exact_state_without_embedding_provider(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    result = sync(conn, None, envelope([claim(), claim("region", "Canada")]))
    assert len(result["constraints"]) == 2
    assert result["memories"][0]["text"] == 'database = "PostgreSQL 17"'
    assert result["pending_embeddings"] == 2
    worker(conn)
    jobs = conn.execute("SELECT * FROM claim_ledger_jobs(10)").fetchall()
    for job in jobs:
        assert conn.execute("SELECT finish_ledger_embedding_job(%s,%s,%s::vector,%s)", (job[0], job[1], VECTOR, "text-embedding-3-small")).fetchone()[0] is True
    conn.execute("RESET ROLE")
    authenticate(conn, ids["hash_a"])
    result = sync(conn, None, query="unmatched", vector=VECTOR)
    assert len(result["constraints"]) == 2
    assert len(result["memories"]) == 2
    assert result["pending_embeddings"] == 0
    assert result["semantic_fallback"] is True
    assert result["ann_candidate_count"] < 8
    assert all(memory["similarity"] == pytest.approx(1) for memory in result["memories"])


def test_all_new_tables_force_rls_and_functions_use_nonlogin_helpers(db):
    conn, _ = db
    rows = conn.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid IN ('immutable_event_log'::regclass,'authoritative_constraints'::regclass,'semantic_embeddings'::regclass,'ledger_outbox'::regclass)").fetchall()
    assert rows == [(True, True)] * 4
    assert conn.execute("SELECT rolcanlogin,rolsuper,rolbypassrls FROM pg_roles WHERE rolname='hivemind_ledger_writer'").fetchone() == (False, False, False)
    assert conn.execute("SELECT has_table_privilege('hivemind_app','authoritative_constraints','INSERT')").fetchone()[0] is False


def test_history_preserves_all_versions_with_explicit_pagination(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, None, envelope())
    sync(conn, None, envelope([claim(value="SQLite", version=1, state="tentative")]))
    history = conn.execute("SELECT query_ledger_history(%s,%s,1,0)", (ids["project_a"], "database")).fetchone()[0]
    assert history["history"][0]["version"] == 2
    assert history["has_more"] is True
    assert history["next_offset"] == 1
    older = conn.execute("SELECT query_ledger_history(%s,%s,1,1)", (ids["project_a"], "database")).fetchone()[0]
    assert older["history"][0]["version"] == 1
    assert older["has_more"] is False


def test_query_attempt_budget_survives_later_sync_conflict(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, None, envelope())
    conn.execute("SELECT reserve_query_embedding_attempt()")
    with pytest.raises(psycopg.errors.SerializationFailure), conn.transaction():
        sync(conn, None, envelope([claim(value="Different", version=0)]))
    assert conn.execute("SELECT value FROM usage_counters WHERE metric='query_embedding_attempts'").fetchone()[0] == 1
    conn.execute("RESET ROLE")
    conn.execute("UPDATE usage_counters SET value=25000 WHERE tenant_id=%s AND metric='query_embedding_attempts'", (ids["tenant_a"],))
    authenticate(conn, ids["hash_a"])
    with pytest.raises(psycopg.errors.RaiseException, match="query_embedding_attempt_limit"), conn.transaction():
        conn.execute("SELECT reserve_query_embedding_attempt()")
    assert conn.execute("SELECT value FROM usage_counters WHERE metric='query_embedding_attempts'").fetchone()[0] == 25000


def test_read_only_key_can_recall_but_cannot_sync_writes(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    sync(conn, None, envelope())
    conn.execute("RESET ROLE")
    conn.execute("UPDATE api_keys SET scopes=ARRAY['memory:read'] WHERE id=%s", (ids["key_a"],))
    authenticate(conn, ids["hash_a"])
    assert sync(conn, None)["constraints"][0]["value"] == "PostgreSQL 17"
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="write_denied"), conn.transaction():
        sync(conn, None, envelope())


def test_missing_conversation_is_recorded_without_fabrication(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    body = envelope()
    body["source"] = {"client": "mcp-host"}
    result = sync(conn, None, body)
    assert result["commit"]["durable"] is True
    assert conn.execute("SELECT source FROM immutable_event_log").fetchone()[0] == {"client": "mcp-host"}


def test_large_historical_memories_are_explicitly_excerpted(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    for i in range(8):
        sync(conn, None, envelope([], fragment=f"needle {i} " + "x" * 15000), query="needle")
    result = sync(conn, None, query="needle")
    assert len(result["memories"]) == 8
    assert all(item["text_truncated"] is True for item in result["memories"])
    assert all(len(item["text"]) == 1000 for item in result["memories"])


def test_history_pages_stop_at_byte_budget_without_losing_versions(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    for version in range(27):
        item = claim(value=f"{version}:" + "v" * 1800, version=version, state="tentative")
        item["evidence"] = "e" * 1800
        sync(conn, None, envelope([item]))
    first = conn.execute("SELECT query_ledger_history(NULL,NULL,50,0)").fetchone()[0]
    assert first["has_more"] is True
    assert len(first["history"]) < 27
    second = conn.execute("SELECT query_ledger_history(NULL,NULL,50,%s)", (first["next_offset"],)).fetchone()[0]
    versions = [item["version"] for item in first["history"] + second["history"]]
    assert versions == list(range(27, 0, -1))
    assert second["has_more"] is False


def seed_filtered_vectors(conn, ids):
    authenticate(conn, ids["hash_a"])
    event_a = sync(conn, None, envelope([], fragment="Seed project A"))["commit"]["event_id"]
    conn.execute("RESET ROLE")
    authenticate(conn, ids["hash_b"])
    event_b = sync(conn, None, envelope([], fragment="Seed project B"))["commit"]["event_id"]
    conn.execute("RESET ROLE")
    insert_query = """INSERT INTO semantic_embeddings
        (id,tenant_id,project_id,event_id,kind,state_at_capture,content,content_digest,embedding)
        VALUES(%s,%s,%s,%s,'fragment','tentative',%s,%s,%s::vector)"""
    expected_ids = set()
    with conn.cursor() as cursor:
        rows = []
        for other_project in (False, True):
            for index in range(200):
                angle = index / 1_000_000 if other_project else 0.1 + index / 1000
                text = f"{'other' if other_project else 'authorized'} document {index}"
                vector = "[1," + str(angle) + "," + ",".join(["0"] * 1534) + "]"
                memory_id = uuid.uuid4()
                if not other_project and index < 8:
                    expected_ids.add(str(memory_id))
                rows.append((
                    memory_id,
                    ids["tenant_b"] if other_project else ids["tenant_a"],
                    ids["project_b"] if other_project else ids["project_a"],
                    event_b if other_project else event_a,
                    text,
                    hashlib.sha256(text.encode()).hexdigest(),
                    vector,
                ))
        for row in rows:
            cursor.execute(insert_query, row)
    conn.execute("ANALYZE semantic_embeddings")
    authenticate(conn, ids["hash_a"])
    return expected_ids


@pytest.mark.skipif(
    os.getenv("HVM_PGLITE") == "1",
    reason="direct filtered HNSW qualification is a native PostgreSQL diagnostic gate",
)
def test_filtered_hnsw_against_exact_recall_with_other_project_distractors(db):
    """Exercise the real filtered index path; this is a recall gate, not a load test."""
    conn, ids = db
    expected = seed_filtered_vectors(conn, ids)
    query = """SELECT id FROM semantic_embeddings
        WHERE project_id=%s AND embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector LIMIT 8"""
    conn.execute("SET LOCAL enable_indexscan=on")
    conn.execute("SET LOCAL enable_bitmapscan=on")
    conn.execute("SET LOCAL enable_seqscan=off")
    conn.execute("SET LOCAL enable_sort=off")
    conn.execute("SET LOCAL hnsw.ef_search=100")
    conn.execute("SET LOCAL hnsw.iterative_scan='strict_order'")
    conn.execute("SET LOCAL hnsw.max_scan_tuples=20000")
    plan = conn.execute("EXPLAIN (FORMAT JSON) " + query, (ids["project_a"], VECTOR)).fetchone()[0]
    assert "vector_knowledge_cosine" in json.dumps(plan)
    approximate = {str(row[0]) for row in conn.execute(query, (ids["project_a"], VECTOR)).fetchall()}
    assert len(approximate) == 8
    assert len(expected & approximate) / len(expected) >= 0.875



def test_sync_exact_fallback_recovers_filtered_semantic_neighbors(db):
    conn, ids = db
    expected = seed_filtered_vectors(conn, ids)
    conn.execute("SET LOCAL enable_indexscan=on")
    conn.execute("SET LOCAL enable_bitmapscan=on")
    conn.execute("SET LOCAL enable_seqscan=off")
    conn.execute("SET LOCAL enable_sort=off")
    response = sync(conn, None, query="unmatched-query-for-semantic-only", vector=VECTOR)
    assert len(response["memories"]) == 8
    returned = {memory["memory_id"] for memory in response["memories"]}
    if response["semantic_fallback"]:
        assert returned == expected
    else:
        assert len(returned & expected) / len(expected) >= 0.875
    assert all(memory["text"].startswith("authorized document") for memory in response["memories"])
    assert response["semantic_fallback"] is (response["ann_candidate_count"] < 8)
    if os.getenv("HVM_PGLITE") == "1":
        assert response["semantic_fallback"] is True
