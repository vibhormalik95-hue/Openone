"""Missing-context calls cannot silently masquerade as completed synchronization."""

from uuid import uuid4

import pytest
from psycopg import errors
from pydantic import ValidationError
from test_ledger import Connection, Embeddings, Pool, accepted_claim

from hivemind.ledger import LedgerEngine, SyncRequest


@pytest.mark.parametrize("payload", [
    {},
    {"query": "Please implement the API"},
    {"checkpoint": {"state": "complete"}},
    {"checkpoint": {"state": "unavailable"}},
    {"checkpoint": {"state": "truncated"}, "idempotency_key": str(uuid4()),
     "source_fragment": "Partial decision only"},
])
async def test_incomplete_context_authenticates_but_cannot_release_dependencies_or_spend(payload):
    connection = Connection()
    embeddings = Embeddings(connection)
    packet = await LedgerEngine(Pool(connection), embeddings).sync(
        SyncRequest.model_validate(payload), "a" * 64,
    )
    assert connection.authentications == 1
    assert packet["status"] == "context_required"
    assert packet["dependency_resolution"] == "blocked"
    assert packet["writes_committed"] is False
    assert "constraints" not in packet and "memories" not in packet
    assert embeddings.calls == 0
    assert all("execute_atomic_sync" not in sql and "reserve_" not in sql
               for sql, _ in connection.queries)


async def test_empty_context_cannot_probe_an_unauthorized_project():
    connection = Connection(denied=True)
    with pytest.raises(errors.InsufficientPrivilege):
        await LedgerEngine(Pool(connection), Embeddings(connection)).sync(SyncRequest(), "a" * 64)


async def test_cold_start_recall_explicitly_declares_no_new_context_and_returns_authority():
    connection = Connection()
    packet = await LedgerEngine(Pool(connection), Embeddings(connection)).sync(
        SyncRequest(checkpoint={"state": "no_new_context"}), "a" * 64,
    )
    assert packet["constraints"] == [{"value": 17}]
    assert packet["dependency_resolution"] == "ready"
    assert packet["checkpoint"]["basis"] == "caller_declared_no_new_context"


@pytest.mark.parametrize("field", ["source_fragment", "fragment"])
def test_fragment_aliases_share_one_stable_sql_idempotency_domain(field):
    identity = str(uuid4())
    request = SyncRequest.model_validate({field: "Use PostgreSQL 17", "idempotency_key": identity})
    assert request.context_ready
    assert request.database_envelope() == {
        "idempotency_key": identity, "source": {"client": "mcp-host"},
        "fragment": "Use PostgreSQL 17", "claims": [],
    }


def test_conflicting_aliases_and_false_no_context_declarations_are_rejected():
    with pytest.raises(ValidationError):
        SyncRequest.model_validate({"fragment": "A", "source_fragment": "B",
                                    "idempotency_key": str(uuid4())})
    with pytest.raises(ValidationError):
        SyncRequest.model_validate({"checkpoint": {"state": "no_new_context"},
                                    "claims": [accepted_claim()], "idempotency_key": str(uuid4())})
    with pytest.raises(ValidationError):
        SyncRequest(checkpoint={"state": "no_new_context", "phase": "post_decision"})


def test_exact_evidence_backed_decision_is_a_delta_without_a_raw_transcript():
    request = SyncRequest.model_validate({"claims": [accepted_claim()],
                                          "idempotency_key": str(uuid4())})
    assert request.context_ready
