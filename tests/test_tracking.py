"""Security and replay checks for the optional development-tracking scripts."""

import hashlib
import hmac
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from github_linear_sync import inbox, init_inbox, verify_webhook  # noqa: E402
from linear_bootstrap import Linear, stable_id, validate  # noqa: E402


def test_valid_signed_webhook():
    raw = json.dumps({"webhookTimestamp": 100_000, "data": {"id": "event"}}).encode()
    signature = hmac.new(b"test-only-signing-key", raw, hashlib.sha256).hexdigest()
    assert verify_webhook(raw, signature, "test-only-signing-key", 100)["data"]["id"] == "event"


def test_signature_rejects_modified_body():
    raw = b'{"webhookTimestamp":100000,"data":{}}'
    signature = hmac.new(b"test-only-signing-key", raw, hashlib.sha256).hexdigest()
    with pytest.raises(ValueError, match="signature"):
        verify_webhook(raw + b" ", signature, "test-only-signing-key", 100)


def test_signed_old_delivery_rejected():
    raw = b'{"webhookTimestamp":100000,"data":{}}'
    signature = hmac.new(b"test-only-signing-key", raw, hashlib.sha256).hexdigest()
    with pytest.raises(ValueError, match="timestamp"):
        verify_webhook(raw, signature, "test-only-signing-key", 161)


def test_stable_ids_are_scoped_and_uuid_v4():
    first = stable_id("team-one", "issue:HM001")
    assert first == stable_id("team-one", "issue:HM001")
    assert first != stable_id("team-two", "issue:HM001")
    assert uuid.UUID(first).version == 4


def test_all_team_pages_are_read():
    calls = []

    class Stub(Linear):
        def call(self, query, variables=None):
            calls.append(variables["after"])
            more = variables["after"] is None
            return {
                "teams": {
                    "nodes": [{"id": "first" if more else "second"}],
                    "pageInfo": {"hasNextPage": more, "endCursor": "cursor-1" if more else None},
                }
            }

    assert [r["id"] for r in Stub("test").pages("teams", "id")] == ["first", "second"]
    assert calls == [None, "cursor-1"]


def test_durable_inbox_replay_is_one_row(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    init_inbox(path)
    for _ in range(2):
        with inbox(path) as db:
            db.execute(
                "INSERT OR IGNORE INTO deliveries(id,issue_id,received) VALUES(?,?,?)",
                ("change-1", "issue-1", 100),
            )
    with inbox(path) as db:
        assert db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1


def test_ticket_graph_complete_and_acyclic():
    data = json.loads((Path(__file__).resolve().parents[1] / "tickets.json").read_text())
    validate(data)
    assert len(data["epics"]) == 6
    assert len(data["tickets"]) == 42
    assert {x["day"] for x in data["tickets"] if x["day"] <= 7} == set(range(1, 8))


def test_ticket_dependency_cycle_rejected():
    data = json.loads((Path(__file__).resolve().parents[1] / "tickets.json").read_text())
    data["tickets"][0]["depends_on"] = [data["tickets"][-1]["key"]]
    with pytest.raises(ValueError, match="cycle"):
        validate(data)
