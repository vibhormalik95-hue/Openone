"""Migration 006 runtime-role invariants, executed against a migrated test database.

These tests prove finite SQL cases and digest recomputation. They are not proofs of
native-client model behavior, cryptographic confidentiality, or database availability.
"""
from __future__ import annotations

import hashlib
import json

import psycopg
import pytest
from psycopg.types.json import Jsonb
from test_database import DSN, VECTOR, authenticate
from test_database import db as database_fixture
from test_unified_database import claim, envelope, seed_filtered_vectors, worker

db = database_fixture
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL for PostgreSQL integration tests")
GENESIS = "0" * 64


def atomic_sync(conn, project=None, body=None, query="PostgreSQL", vector=None):
    return conn.execute(
        "SELECT public.execute_atomic_sync(%s,%s,%s::vector,%s)",
        (project, query, vector, Jsonb(body) if body is not None else None),
    ).fetchone()[0]


def audit(conn, project=None, after=0, limit=50):
    return conn.execute("SELECT query_project_audit(%s,%s,%s)", (project, after, limit)).fetchone()[0]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_entry(entry, previous):
    commitment = json.loads(entry["canonical_commitment"])
    assert entry["previous_hash"] == previous
    assert commitment["previous_hash"] == previous
    assert commitment["sequence"] == entry["sequence"]
    assert commitment["source_id"] == entry["source_id"]
    assert commitment["row_digest"] == digest(entry["canonical_row"])
    assert entry["source_content_digest"] == digest(entry["canonical_source"])
    assert commitment["source_content_digest"] == entry["source_content_digest"]
    assert entry["chain_hash"] == digest(entry["canonical_commitment"])
    return entry["chain_hash"]


def test_requested_tables_are_canonical_physical_rls_tables(db):
    conn, _ = db
    rows = conn.execute("""SELECT relname,relkind,relrowsecurity,relforcerowsecurity FROM pg_class
        WHERE oid IN ('immutable_event_log'::regclass,'authoritative_constraints'::regclass,
        'semantic_embeddings'::regclass,'project_audit_chain'::regclass) ORDER BY relname""").fetchall()
    assert len(rows) == 4
    assert all(row[1:] == ("r", True, True) for row in rows)
    for name in ("memory_events", "ledger_constraints", "vector_knowledge", "authoritative_constraint_states"):
        options = conn.execute("SELECT relkind,reloptions FROM pg_class WHERE oid=%s::regclass", (name,)).fetchone()
        assert options[0] == "v"
        assert "security_invoker=true" in options[1]
        assert "security_barrier=true" in options[1]
    assert conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname='vector_knowledge_cosine'").fetchone()[0].find("ON public.semantic_embeddings USING hnsw") >= 0


def test_hash_chain_covers_event_and_all_immutable_revision_fields(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    response = atomic_sync(conn, body=envelope([claim(), claim("jwt-rotation", 12)]))
    entries = audit(conn)["entries"]
    assert [entry["entry_type"] for entry in entries] == ["event", "ledger_revision", "ledger_revision"]
    previous = GENESIS
    for sequence, entry in enumerate(entries, 1):
        assert entry["sequence"] == sequence
        assert entry["historical_backfill"] is False
        previous = verify_entry(entry, previous)
    assert response["audit_checkpoint"] == {"sequence": 3, "chain_hash": previous}
    receipt = response["invocation_receipt"]
    assert digest(receipt["canonical_receipt"]) == receipt["receipt_hash"]
    assert json.loads(receipt["canonical_receipt"])["chain_hash"] == previous
    assert receipt["event_id"] == response["commit"]["event_id"]
    assert receipt["durably_stored"] is False
    assert digest(entries[1]["canonical_row"].replace("PostgreSQL", "MySQL")) != json.loads(entries[1]["canonical_commitment"])["row_digest"]


def test_receipt_binds_exact_base_response_and_state(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    response = atomic_sync(conn, body=envelope())
    # The application role cannot invoke unrestricted private digest helpers.
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("SELECT ledger_digest(%s)", (Jsonb({"test": True}),))
    conn.execute("RESET ROLE")
    base = {k: v for k, v in response.items() if k not in ("audit_checkpoint", "invocation_receipt")}
    actual = conn.execute("SELECT ledger_digest(%s),ledger_digest(%s)", (Jsonb(base), Jsonb(base["authoritative_state"]))).fetchone()
    assert actual == (response["invocation_receipt"]["response_digest"], response["invocation_receipt"]["state_digest"])


def test_active_superseded_retracted_are_derived_without_history_updates(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    atomic_sync(conn, body=envelope([claim("jwt-rotation", 12)]))
    atomic_sync(conn, body=envelope([claim("jwt-rotation", 8, 1, "tentative")]))
    assert conn.execute("SELECT version,lifecycle_state FROM authoritative_constraint_states ORDER BY version").fetchall() == [(1, "active"), (2, "tentative")]
    atomic_sync(conn, body=envelope([claim("jwt-rotation", 6, 2)]))
    assert conn.execute("SELECT version,lifecycle_state FROM authoritative_constraint_states ORDER BY version").fetchall() == [(1, "superseded"), (2, "tentative"), (3, "active")]
    atomic_sync(conn, body=envelope([claim("jwt-rotation", 6, 3, "retracted")]))
    assert conn.execute("SELECT version,lifecycle_state FROM authoritative_constraint_states ORDER BY version").fetchall() == [(1, "superseded"), (2, "tentative"), (3, "superseded"), (4, "retracted")]
    assert atomic_sync(conn)["constraints"] == []
    assert conn.execute("SELECT value FROM authoritative_constraints WHERE version=1").fetchone()[0] == 12


def test_idempotent_replay_and_conflict_do_not_fork_audit_chain(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    body = envelope()
    first = atomic_sync(conn, body=body)
    replay = atomic_sync(conn, body=body)
    assert replay["commit"]["replayed"] is True
    assert first["audit_checkpoint"] == replay["audit_checkpoint"]
    assert first["invocation_receipt"]["invocation_id"] != replay["invocation_receipt"]["invocation_id"]
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        atomic_sync(conn, body=dict(body, claims=[claim(value="MySQL")]))
    with pytest.raises(psycopg.errors.SerializationFailure), conn.transaction():
        atomic_sync(conn, body=envelope([claim(value="MySQL", version=0)]))
    assert audit(conn)["checkpoint"] == first["audit_checkpoint"]


def test_actual_application_role_denies_cross_tenant_and_private_project(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    atomic_sync(conn, body=envelope())
    assert conn.execute("SELECT current_user").fetchone()[0] == "hivemind_app"
    conn.execute("RESET ROLE")
    authenticate(conn, ids["hash_b"])
    for relation in ("immutable_event_log", "authoritative_constraints", "semantic_embeddings", "project_audit_chain", "authoritative_constraint_states", "memory_events", "ledger_constraints", "vector_knowledge"):
        assert conn.execute(psycopg.sql.SQL("SELECT count(*) FROM {} WHERE project_id=%s").format(psycopg.sql.Identifier(relation)), (ids["project_a"],)).fetchone()[0] == 0
    for call in (lambda: atomic_sync(conn, ids["project_a"]), lambda: atomic_sync(conn, ids["project_a"], envelope()), lambda: audit(conn, ids["project_a"])):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="project_access_denied"), conn.transaction():
            call()
    conn.execute("RESET ROLE")
    authenticate(conn, ids["hash_a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        atomic_sync(conn, ids["private"])
    conn.execute("SELECT set_config('app.api_key_id',%s,true)", (str(ids["key_b"]),))
    assert conn.execute("SELECT count(*) FROM project_audit_chain").fetchone()[0] == 0


def test_runtime_cannot_write_chain_or_invoke_internal_audit_helpers(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    for statement in ("INSERT INTO project_audit_chain DEFAULT VALUES", "DELETE FROM project_audit_chain", "SELECT append_project_audit('event','{}',false)"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(statement)
    assert conn.execute("SELECT rolcanlogin,rolsuper,rolbypassrls FROM pg_roles WHERE rolname='hivemind_audit_writer'").fetchone() == (False, False, False)
    assert conn.execute("SELECT pg_has_role('hivemind_app','hivemind_audit_writer','MEMBER')").fetchone()[0] is False


def test_audit_immutability_denies_ordinary_administrator_mutations(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    atomic_sync(conn, body=envelope())
    conn.execute("RESET ROLE")
    for statement in ("UPDATE project_audit_chain SET chain_hash=repeat('a',64)", "DELETE FROM project_audit_chain", "TRUNCATE project_audit_chain"):
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="immutable_history"), conn.transaction():
            conn.execute(statement)


def test_worker_tentative_insert_is_captured_by_same_project_chain(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    atomic_sync(conn, body=envelope([], fragment="Consider SQLite for a disposable test."))
    worker(conn)
    jobs = conn.execute("SELECT * FROM claim_ledger_jobs(10)").fetchall()
    job = next(row for row in jobs if row[2] == "extraction")
    assert conn.execute("SELECT finish_ledger_extraction_job(%s,%s,%s,%s)", (job[0], job[1], Jsonb([claim(value="SQLite", state="tentative")]), "deterministic-test-extractor")).fetchone()[0] is True
    conn.execute("RESET ROLE")
    authenticate(conn, ids["hash_a"])
    rows = audit(conn)["entries"]
    assert len(rows) == 2
    revision = json.loads(rows[1]["canonical_row"])
    assert revision["origin"] == "extractor"
    assert revision["state"] == "tentative"
    verify_entry(rows[1], verify_entry(rows[0], GENESIS))
    assert atomic_sync(conn)["constraints"] == []


def test_audit_pagination_and_empty_genesis_are_explicit(db):
    conn, ids = db
    authenticate(conn, ids["hash_a"])
    empty = audit(conn)
    assert empty["entries"] == []
    assert empty["checkpoint"] == {"sequence": 0, "chain_hash": GENESIS}
    atomic_sync(conn, body=envelope())
    first = audit(conn, limit=1)
    second = audit(conn, after=first["next_sequence"], limit=1)
    assert first["has_more"] is True
    assert second["has_more"] is False
    assert second["entries"][0]["previous_hash"] == first["entries"][0]["chain_hash"]
    with pytest.raises(psycopg.errors.InvalidParameterValue), conn.transaction():
        audit(conn, after=-1)


def test_atomic_wrapper_retains_filtered_exact_fallback_and_audit_boundary(db):
    conn, ids = db
    expected = seed_filtered_vectors(conn, ids)
    conn.execute("SET LOCAL enable_indexscan=on")
    conn.execute("SET LOCAL enable_bitmapscan=on")
    conn.execute("SET LOCAL enable_seqscan=off")
    conn.execute("SET LOCAL enable_sort=off")
    response = atomic_sync(conn, query="semantic-only-unmatched", vector=VECTOR)
    returned = {memory["memory_id"] for memory in response["memories"]}
    assert len(returned) == 8
    if response["semantic_fallback"]:
        assert returned == expected
    else:
        assert len(returned & expected) / 8 >= 0.875
    assert all(memory["text"].startswith("authorized document") for memory in response["memories"])
    assert response["audit_checkpoint"]["sequence"] == 1
