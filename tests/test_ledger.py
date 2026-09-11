"""Focused application contracts, canonicalization, and authorization boundaries."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from psycopg import errors
from pydantic import ValidationError

from hivemind.engine import EmbeddingUnavailable
from hivemind.ledger import Claim, LedgerEngine, ManageRequest, SyncRequest, payload_sha256


def accepted_claim(**updates):
    return {
        "entity_key": "database.version", "kind": "constraint", "state": "accepted",
        "value": {"engine": "PostgreSQL", "version": 17}, "expected_version": 0,
        "evidence": "Use PostgreSQL 17.", **updates,
    }


def write_request(**updates):
    return {
        "idempotency_key": str(uuid4()),
        "source": {"client": "pytest", "conversation_id": "turn-1"},
        "claims": [accepted_claim()], **updates,
    }


def test_canonical_checksum_preserves_meaning_and_normalizes_syntax():
    assert payload_sha256({"a": "cafe\u0301\r\n", "b": 1}) == payload_sha256(
        {"b": 1, "a": "café\n"},
    )
    assert payload_sha256("Hello") != payload_sha256("hello")
    assert payload_sha256("two  spaces") != payload_sha256("two spaces")
    assert payload_sha256(" trailing ") != payload_sha256("trailing")
    with pytest.raises(ValueError, match="collide"):
        payload_sha256({"café": 1, "cafe\u0301": 2})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "nul\x00text", "\ud800"])
def test_values_reject_database_incompatible_data(value):
    with pytest.raises((ValidationError, ValueError)):
        Claim.model_validate(accepted_claim(value=value))


@pytest.mark.parametrize("updates", [
    {"tenant_id": str(uuid4())},
    {"claims": [accepted_claim(expected_version=True)]},
    {"claims": [accepted_claim(), accepted_claim()]},
    {"claims": [accepted_claim(state="retracted")]},
    {"claims": [accepted_claim(value="x" * 4097)]},
    {"fragment": "\n "},
    {"idempotency_key": None},
])
def test_unsafe_sync_requests_are_rejected(updates):
    with pytest.raises(ValidationError):
        SyncRequest.model_validate(write_request(**updates))


def test_pure_recall_needs_no_identity_arguments_or_write_metadata():
    request = SyncRequest()
    assert request.project_id is None
    assert request.database_envelope() is None
    with pytest.raises(ValidationError):
        SyncRequest(source={"client": "pytest", "conversation_id": "thread"})


def test_write_payload_preserves_stable_idempotency_and_claim_provenance():
    request = SyncRequest.model_validate(write_request())
    envelope = request.database_envelope()
    assert envelope["idempotency_key"] == str(request.idempotency_key)
    assert envelope["claims"][0]["evidence"] == "Use PostgreSQL 17."
    assert set(envelope) == {"source", "fragment", "claims", "idempotency_key"}


def test_unavailable_native_conversation_identifiers_are_omitted_without_fabrication():
    request = SyncRequest.model_validate(write_request(source=None))
    assert request.database_envelope()["source"] == {"client": "mcp-host"}


@pytest.mark.parametrize("updates", [
    {"query": "字" * 1400},
    {"fragment": "字" * 6000},
    {"claims": [accepted_claim(evidence="字" * 700)]},
    {"claims": [accepted_claim(value={str(i): i for i in range(250)})]},
    {"claims": [accepted_claim(expected_version=1_000_000_000)]},
])
def test_unicode_jsonb_and_revision_limits_match_database(updates):
    with pytest.raises(ValidationError):
        SyncRequest.model_validate(write_request(**updates))


@pytest.mark.parametrize("updates", [
    {"action": "history", "claim": accepted_claim()},
    {"action": "override"},
    {"action": "history", "limit": 51},
    {"action": "history", "offset": 10001},
    {"action": "history", "limit": True},
])
def test_manual_management_has_bounded_unambiguous_actions(updates):
    with pytest.raises(ValidationError):
        ManageRequest.model_validate(updates)


class Cursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class Connection:
    def __init__(self, *, denied=False, exhausted=False, stale=False, write_denied=False):
        self.project_id = uuid4()
        self.denied, self.exhausted, self.stale = denied, exhausted, stale
        self.write_denied = write_denied
        self.queries = []
        self.transactions = 0
        self.authentications = 0
        self.in_transaction = False

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    async def execute(self, query, params=()):
        assert self.in_transaction
        self.queries.append((query, params))
        if "authenticate_api_key" in query:
            self.authentications += 1
            return Cursor({"key_id": uuid4(), "tenant_id": uuid4()})
        if "resolve_authorized_project" in query:
            if self.denied:
                raise errors.InsufficientPrivilege("project_access_denied")
            return Cursor({"project_id": self.project_id})
        if "ensure_recall_allowance" in query:
            if self.exhausted:
                raise errors.RaiseException("monthly_usage_limit")
            return Cursor(None)
        if "can_access_project" in query:
            return Cursor({"allowed": not self.write_denied})
        if "reserve_query_embedding_attempt" in query:
            return Cursor(None)
        if "execute_atomic_sync" in query:
            if self.stale:
                raise errors.SerializationFailure("ledger_version_conflict")
            return Cursor({"result": {"constraints": [{"value": 17}], "memories": []}})
        if "query_ledger_history" in query:
            return Cursor({"result": {"history": []}})
        raise AssertionError(query)


class Pool:
    def __init__(self, connection):
        self.instance = connection

    @asynccontextmanager
    async def connection(self):
        yield self.instance


class Embeddings:
    def __init__(self, connection, outcome="ready"):
        self.connection, self.outcome = connection, outcome
        self.calls = 0

    async def embed(self, text):
        assert not self.connection.in_transaction, "provider I/O must not hold a transaction"
        self.calls += 1
        if self.outcome == "outage":
            raise EmbeddingUnavailable("provider_unavailable")
        if self.outcome == "timeout":
            await asyncio.sleep(1)
        if self.outcome == "bad_vector":
            return [float("nan")] * 1536
        return [1.0] + [0.0] * 1535


@pytest.mark.parametrize("denied,exhausted,error", [
    (True, False, errors.InsufficientPrivilege), (False, True, errors.RaiseException),
])
async def test_unauthorized_or_exhausted_requests_never_call_paid_provider(denied, exhausted, error):
    connection = Connection(denied=denied, exhausted=exhausted)
    provider = Embeddings(connection)
    engine = LedgerEngine(Pool(connection), provider)
    with pytest.raises(error):
        await engine.sync(SyncRequest(checkpoint={"state": "no_new_context"}), "a" * 64)
    assert provider.calls == 0


@pytest.mark.parametrize("outcome", ["outage", "timeout", "bad_vector"])
async def test_query_provider_outage_keeps_atomic_write_and_exact_recall(outcome):
    connection = Connection()
    engine = LedgerEngine(Pool(connection), Embeddings(connection, outcome), 0.01)
    request = SyncRequest.model_validate(write_request())
    packet = await engine.sync(request, "a" * 64)
    assert packet["constraints"] == [{"value": 17}]
    assert packet["semantic_status"] == "unavailable"
    assert connection.authentications == 2
    sync_call = next(call for call in connection.queries if "execute_atomic_sync" in call[0])
    assert sync_call[1][0] == connection.project_id
    assert sync_call[1][2] is None
    assert sync_call[1][3].obj["idempotency_key"] == str(request.idempotency_key)
    assert packet["request_payload_digest_scope"] == "python_canonical_wire_payload_v1"


async def test_version_conflict_is_not_silently_retried_with_new_version():
    connection = Connection(stale=True)
    engine = LedgerEngine(Pool(connection), Embeddings(connection))
    with pytest.raises(errors.SerializationFailure):
        await engine.sync(SyncRequest.model_validate(write_request()), "a" * 64)
    assert len([query for query, _ in connection.queries if "execute_atomic_sync" in query]) == 1
    assert len([query for query, _ in connection.queries if "reserve_query_embedding_attempt" in query]) == 1


async def test_read_only_key_cannot_spend_provider_tokens_on_invalid_write():
    connection = Connection(write_denied=True)
    provider = Embeddings(connection)
    engine = LedgerEngine(Pool(connection), provider)
    with pytest.raises(errors.InsufficientPrivilege, match="write_access_denied"):
        await engine.sync(SyncRequest.model_validate(write_request()), "a" * 64)
    assert provider.calls == 0
    assert all("reserve_query_embedding_attempt" not in query for query, _ in connection.queries)


async def test_history_uses_bound_parameters_and_never_calls_embedding_provider():
    connection = Connection()
    embeddings = Embeddings(connection)
    engine = LedgerEngine(Pool(connection), embeddings)
    await engine.manage(ManageRequest(action="history", entity_key="database.version", limit=5), "a" * 64)
    query, parameters = connection.queries[-1]
    assert "query_ledger_history(%s, %s, %s, %s)" in query
    assert parameters == (connection.project_id, "database.version", 5, 0)
    assert embeddings.calls == 0
