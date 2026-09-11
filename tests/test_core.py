import json
import logging
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hivemind.auth import TOKEN_PATTERN, token_digest
from hivemind.engine import EmbeddingUnavailable, OpenAIEmbeddings
from hivemind.models import CommitMemoryRequest, RecallMemoryRequest
from hivemind.observability import RedactedJsonFormatter, scrub_sentry


def commit_payload():
    return {
        "project_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "source": {"client": "claude-code", "conversation_id": "thread-123"},
        "memories": [{"kind": "decision", "text": "Use PostgreSQL as canonical state."}],
        "constraints": [{"key": "db.version", "value": 17, "expected_version": 0}],
    }


def test_commit_schema_accepts_uuid_strings_and_preserves_exact_evidence():
    payload = commit_payload()
    payload["memories"][0]["text"] = "  Exact quotation remains exact.  "
    model = CommitMemoryRequest.model_validate(payload)
    assert model.database_payload()["memories"][0]["text"] == payload["memories"][0]["text"]
    assert "project_id" not in model.database_payload()


@pytest.mark.parametrize("case", ["extra", "bool_version", "empty", "duplicate", "big_metadata"])
def test_invalid_commit_batches_are_rejected_before_database(case):
    payload = commit_payload()
    if case == "extra":
        payload["tenant_id"] = str(uuid4())
    elif case == "bool_version":
        payload["constraints"][0]["expected_version"] = True
    elif case == "empty":
        payload["constraints"], payload["memories"] = [], []
    elif case == "duplicate":
        payload["constraints"] *= 2
    elif case == "big_metadata":
        payload["memories"][0]["metadata"] = {"data": "x" * 2049}
    with pytest.raises(ValidationError):
        CommitMemoryRequest.model_validate(payload)


@pytest.mark.parametrize("limit", [0, 21, True, "8"])
def test_recall_rejects_unbounded_or_coerced_limits(limit):
    with pytest.raises(ValidationError):
        RecallMemoryRequest(project_id=str(uuid4()), query="runtime choice", limit=limit)


def test_auth_token_hash_not_reversible_or_ambiguous():
    token = "hvm_" + "a" * 43
    assert TOKEN_PATTERN.fullmatch(token)
    assert not TOKEN_PATTERN.fullmatch("USR_SEC_embedded-in-url")
    assert len(token_digest(token)) == 64
    assert token_digest(token) != token_digest(token + "b")


def test_logs_and_sentry_strip_secrets_and_memory():
    secret = "hvm_" + "x" * 43
    record = logging.LogRecord("psycopg.pool", logging.ERROR, "db.py", 1, secret, (), None)
    assert secret not in RedactedJsonFormatter().format(record)
    event = {"request": {"headers": {"authorization": secret}}, "breadcrumbs": [secret],
             "user": {"email": "private@example.test"}, "extra": {"body": secret},
             "exception": {"values": [{"type": "Exception", "value": secret,
                 "stacktrace": {"frames": [{"vars": {"token": secret}}]}}]}}
    assert secret not in json.dumps(scrub_sentry(event, None))


async def test_unconfigured_embeddings_fail_explicitly():
    provider = OpenAIEmbeddings("")
    with pytest.raises(EmbeddingUnavailable, match="not_configured"):
        await provider.embed("semantic query")
    await provider.close()


async def test_recall_authorization_and_quota_are_checked_before_paid_embedding():
    from contextlib import asynccontextmanager

    from psycopg import errors

    from hivemind.engine import MemoryEngine

    class Cursor:
        def __init__(self, row):
            self.row = row

        async def fetchone(self):
            return self.row

    class Connection:
        def __init__(self, allowed, exhausted):
            self.allowed, self.exhausted = allowed, exhausted

        @asynccontextmanager
        async def transaction(self):
            yield

        async def execute(self, query, params=()):
            if "authenticate_api_key" in query:
                return Cursor({"key_id": uuid4(), "tenant_id": uuid4()})
            if "can_access_project" in query:
                return Cursor({"allowed": self.allowed})
            if "ensure_recall_allowance" in query:
                if self.exhausted:
                    raise errors.RaiseException("monthly_usage_limit")
                return Cursor(None)
            raise AssertionError("Retrieval must not occur after rejected preflight")

    class Pool:
        def __init__(self, connection):
            self.connection_instance = connection

        @asynccontextmanager
        async def connection(self):
            yield self.connection_instance

    class Provider:
        calls = 0

        async def embed(self, query):
            self.calls += 1
            raise AssertionError("Denied calls must not spend embedding tokens")

    request = RecallMemoryRequest(project_id=str(uuid4()), query="architecture")
    for allowed, exhausted, error in [(False, False, errors.InsufficientPrivilege),
                                       (True, True, errors.RaiseException)]:
        provider = Provider()
        engine = MemoryEngine(Pool(Connection(allowed, exhausted)), provider)
        with pytest.raises(error):
            await engine.recall(request, "a" * 64)
        assert provider.calls == 0


def test_oauth_legacy_transport_combination_is_rejected_before_state_mutation():
    from hivemind.app import create_app
    from hivemind.config import Settings

    with pytest.raises(ValueError, match="Legacy SSE"):
        create_app(Settings(auth_mode="oauth", enable_legacy_sse=True))
