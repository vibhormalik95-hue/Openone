"""Authenticated HTTP/SQL workflow proof with explicit simulation boundaries.

Use a disposable migrated database. Set INVESTOR_ADMIN_DATABASE_URL and
INVESTOR_APP_DATABASE_URL; native app credentials must log in as hivemind_app.
The harness creates isolated synthetic tenants and retains their immutable audit
rows until the disposable database is removed. No real client, OAuth exchange,
model inference, payment, TLS or elapsed ten-minute delay is simulated as real.
"""
from __future__ import annotations

if not __debug__:
    raise RuntimeError("Proof execution requires Python assertions; do not use -O or PYTHONOPTIMIZE")

import argparse
import asyncio
import base64
import copy
import hashlib
import json
import math
import os
import re
import secrets
import socket
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import uvicorn
from asgi_lifespan import LifespanManager
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from hivemind.app import create_app
from hivemind.auth import ApiKeyVerifier, authenticated_connection, token_digest
from hivemind.config import EMBEDDING_DIMENSIONS, Settings
from hivemind.ledger import LedgerEngine

ROOT = Path(__file__).resolve().parents[1]
CLASSIC = "2025-11-25"
MODERN = "2026-07-28"
INITIAL = "DB must use PostgreSQL 17 with strict serializable isolation; JWT secrets rotate every 12 hours"
UPDATED = "DB must use PostgreSQL 17 with strict serializable isolation; JWT secrets rotate every 6 hours"
ZERO_HASH = "0" * 64


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class DeterministicFixtureEmbeddings:
    """Hash-based test vectors exercise SQL transport, not semantic model quality."""

    async def embed(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        for token in re.findall(r"\w+", text.lower()):
            position = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big") % len(vector)
            vector[position] += 1.0
        if not any(vector):
            vector[0] = 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]


def make_claim(value: str, version: int) -> dict:
    return {"entity_key": "architecture", "kind": "constraint", "state": "accepted",
            "value": value, "expected_version": version, "evidence": value}


def project_constraint(packet: dict) -> dict:
    matches = [item for item in packet.get("constraints", []) if item["entity_key"] == "architecture"]
    assert len(matches) == 1, "Expected exactly one authoritative architecture constraint"
    assert packet.get("constraints_complete") is True
    return matches[0]


def build_grounded_api_schema(packet: dict) -> dict:
    """A deterministic fixture compiler, deliberately not a model reasoning claim."""
    constraint = project_constraint(packet)
    match = re.fullmatch(
        r"DB must use PostgreSQL (\d+) with strict serializable isolation; JWT secrets rotate every (\d+) hours",
        constraint["value"],
    )
    assert match is not None, "Unrecognized contract: fail closed instead of guessing"
    postgres_version, rotation_hours = map(int, match.groups())
    return {
        "openapi": "3.1.0", "info": {"title": "Fixture Architecture Contract", "version": "1.0.0"},
        "paths": {"/architecture": {"get": {"responses": {"200": {"description": "Authoritative configuration",
            "content": {"application/json": {"schema": {"type": "object", "additionalProperties": False,
                "required": ["postgres_major", "transaction_isolation", "jwt_secret_rotation_seconds"],
                "properties": {"postgres_major": {"type": "integer", "const": postgres_version},
                    "transaction_isolation": {"type": "string", "const": "SERIALIZABLE"},
                    "jwt_secret_rotation_seconds": {"type": "integer", "const": rotation_hours * 3600}}}}}}}}}},
        "x-hivemind-source": {"entity_key": "architecture", "version": constraint["version"],
            "exact_value": constraint["value"], "value_sha256": hashlib.sha256(constraint["value"].encode()).hexdigest()},
        "x-proof-generator": "deterministic contract fixture; no live model",
    }


def decode_rpc(response: httpx.Response) -> dict:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise AssertionError("HTTP stream contained no JSON-RPC response")
    return response.json()


@asynccontextmanager
async def live_http(app):
    """Run the real HTTP server on a private loopback ephemeral port."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False,
                                          log_level="warning", log_config=None))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()
                    raise RuntimeError("HTTP server exited before readiness")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(10):
                await task
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise RuntimeError("HTTP server shutdown deadline exceeded") from None
        finally:
            listener.close()


class Workflow:
    def __init__(self, client: httpx.AsyncClient, tokens: dict[str, str]):
        self.client = client
        self.tokens = tokens
        self.records: list[dict] = []

    async def rpc(self, *, agent: str, protocol: str, method: str, params: dict | None = None,
                  purpose: str, tenant: str = "A", logical_offset: int = 0) -> dict:
        params = copy.deepcopy(params or {})
        credential_label = tenant if tenant == "B" else {"simulated-chatgpt": "A2", "simulated-cursor": "A3"}.get(agent, "A")
        headers = {"Authorization": f"Bearer {self.tokens[credential_label]}",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": protocol}
        if protocol == MODERN:
            params["_meta"] = {"io.modelcontextprotocol/protocolVersion": protocol,
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": agent, "version": "proof-fixture-1"}}
            headers["Mcp-Method"] = method
            if method == "tools/call":
                headers["Mcp-Name"] = params["name"]
        request = {"jsonrpc": "2.0", "id": len(self.records) + 1, "method": method, "params": params}
        started = utc_now()
        response = await self.client.post("/mcp/v1", headers=headers, json=request)
        body = decode_rpc(response)
        record = {"index": len(self.records) + 1, "agent": agent, "tenant_label": tenant,
                  "protocol": protocol, "purpose": purpose, "credential_label": credential_label, "logical_offset_seconds": logical_offset,
                  "started_at": started, "completed_at": utc_now(), "http_status": response.status_code,
                  "request": request, "response": body,
                  "previous_hash": self.records[-1]["receipt_hash"] if self.records else ZERO_HASH}
        record["receipt_hash"] = digest(record)
        self.records.append(record)
        assert response.status_code == 200, f"{purpose}: unexpected HTTP {response.status_code}"
        return body

    async def sync(self, *, request: dict, purpose: str, agent: str = "simulated-claude-code",
                   protocol: str = CLASSIC, tenant: str = "A", logical_offset: int = 0,
                   expect_error: bool = False) -> dict:
        body = await self.rpc(agent=agent, protocol=protocol, method="tools/call",
                              params={"name": "sync_context", "arguments": {"request": request}},
                              purpose=purpose, tenant=tenant, logical_offset=logical_offset)
        result = body.get("result", {})
        failed = bool(body.get("error") or result.get("isError"))
        assert failed == expect_error, f"{purpose}: unexpected tool status {body}"
        return result.get("structuredContent", result)


async def seed(admin_dsn: str) -> tuple[dict, dict]:
    ids = {name: uuid4() for name in ("tenant_a", "tenant_b", "project_a", "project_b", "key_a", "key_a2", "key_a3", "key_b")}
    tokens = {label: "hvm_" + secrets.token_urlsafe(32) for label in ("A", "A2", "A3", "B")}
    async with await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True) as connection:
        if os.getenv("HVM_PGLITE") == "1":
            await connection.execute("RESET ROLE")
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO tenants(id,name,billing_status) VALUES(%s,%s,'active'),(%s,%s,'active')",
                (ids["tenant_a"], "Investor proof synthetic tenant A", ids["tenant_b"], "Investor proof synthetic tenant B"))
            await connection.execute(
                "INSERT INTO projects(id,tenant_id,name,slug) VALUES(%s,%s,'Synthetic A','proof-a'),(%s,%s,'Synthetic B','proof-b')",
                (ids["project_a"], ids["tenant_a"], ids["project_b"], ids["tenant_b"]))
            for label, key_name, tenant_name, project_name in (
                ("A", "key_a", "tenant_a", "project_a"), ("A2", "key_a2", "tenant_a", "project_a"),
                ("A3", "key_a3", "tenant_a", "project_a"), ("B", "key_b", "tenant_b", "project_b"),
            ):
                await connection.execute(
                    "INSERT INTO api_keys(id,tenant_id,key_hash,prefix) VALUES(%s,%s,%s,%s)",
                    (ids[key_name], ids[tenant_name], token_digest(tokens[label]), "hvm_proof_" + label.lower()))
                await connection.execute(
                    "INSERT INTO api_key_projects(tenant_id,api_key_id,project_id) VALUES(%s,%s,%s)",
                    (ids[tenant_name], ids[key_name], ids[project_name]))
    return ids, tokens


async def run_proof() -> dict:
    admin_dsn = os.getenv("INVESTOR_ADMIN_DATABASE_URL", "")
    app_dsn = os.getenv("INVESTOR_APP_DATABASE_URL", "")
    if not admin_dsn or not app_dsn:
        raise RuntimeError("Set INVESTOR_ADMIN_DATABASE_URL and INVESTOR_APP_DATABASE_URL for a disposable migrated test database")
    started = utc_now()
    ids, tokens = await seed(admin_dsn)
    portable = os.getenv("HVM_PGLITE") == "1"

    async def configure(connection):
        await register_vector_async(connection)
        if portable:
            await connection.execute("SET ROLE hivemind_app")
        row = await (await connection.execute(
            "SELECT current_user AS role, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")).fetchone()
        assert row == {"role": "hivemind_app", "rolsuper": False, "rolbypassrls": False}, row
        await connection.commit()

    pool = AsyncConnectionPool(app_dsn, min_size=1, max_size=1, open=False, configure=configure,
                               kwargs={"row_factory": dict_row, "connect_timeout": 10})
    engine = LedgerEngine(pool, DeterministicFixtureEmbeddings())
    settings = Settings(public_base_url="http://testserver", db_pool_max_size=1)
    app = create_app(settings, pool=pool, engine=engine, auth=ApiKeyVerifier(pool))
    assertions: list[str] = []
    bundle: dict = {}
    async with LifespanManager(app), live_http(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30, trust_env=False) as client:
            workflow = Workflow(client, tokens)
            for agent in ("simulated-claude-code", "simulated-cursor"):
                response = await workflow.rpc(agent=agent, protocol=CLASSIC, method="initialize", purpose="classic_initialize",
                    params={"protocolVersion": CLASSIC, "capabilities": {}, "clientInfo": {"name": agent, "version": "proof-fixture-1"}})
                assert response["result"]["protocolVersion"] == CLASSIC
                assert response["result"]["instructions"]
            discovery = await workflow.rpc(agent="simulated-chatgpt", protocol=MODERN, method="server/discover", purpose="modern_discovery")
            assert discovery.get("result") and not discovery.get("error"), discovery
            assertions.append("Classic initialize and modern server/discover both pass through the production ASGI application")
            for protocol, agent in ((CLASSIC, "simulated-claude-code"), (MODERN, "simulated-chatgpt")):
                listed = await workflow.rpc(agent=agent, protocol=protocol, method="tools/list", purpose="tool_discovery")
                assert {item["name"] for item in listed["result"]["tools"]} == {"sync_context", "manage_ledger"}
            empty_body = await workflow.rpc(agent="simulated-claude-code", protocol=CLASSIC, method="tools/call",
                params={"name": "sync_context", "arguments": {}}, purpose="empty_arguments_context_challenge")
            missing = empty_body["result"]["structuredContent"]
            assert missing["checkpoint"]["status"] == "needs_context"
            assert missing["dependency_resolution"] == "blocked" and not missing.get("constraints")
            truncated = await workflow.sync(request={"checkpoint": {"state": "truncated"}}, purpose="truncated_context_challenge")
            assert truncated["checkpoint"]["status"] == "needs_context"
            assert truncated["dependency_resolution"] == "blocked" and not truncated.get("constraints")
            assertions.append("Empty tool arguments and explicitly truncated context return authenticated challenges with no authoritative context")
            initial_request = {"idempotency_key": str(uuid4()), "source": {"client": "simulated-claude-code"},
                "query": "Establish the accepted architecture", "claims": [make_claim(INITIAL, 0)]}
            initial = await workflow.sync(request=initial_request, purpose="agent1_initial_acceptance")
            assert project_constraint(initial)["value"] == INITIAL
            assert project_constraint(initial)["version"] == 1
            assert initial["dependency_resolution"] == "ready"
            replay = await workflow.sync(request=initial_request, purpose="agent1_identical_retry")
            assert replay["commit"]["event_id"] == initial["commit"]["event_id"]
            assert replay["commit"]["replayed"] is True
            assertions.append("Identical request replay preserves event identity and version")
            fresh = await workflow.sync(request={"query": "Return the exact current architecture", "checkpoint": {"state": "no_new_context"}},
                purpose="agent2_zero_history_recall", agent="simulated-chatgpt", protocol=MODERN, logical_offset=600)
            assert project_constraint(fresh)["value"] == INITIAL
            assert project_constraint(fresh)["version"] == 1
            api_schema = build_grounded_api_schema(fresh)
            props = api_schema["paths"]["/architecture"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
            assert props["postgres_major"]["const"] == 17
            assert props["transaction_isolation"]["const"] == "SERIALIZABLE"
            assert props["jwt_secret_rotation_seconds"]["const"] == 43200
            assertions.append("Fresh scripted Agent 2 receives the exact accepted value and deterministically compiles an adhering API schema")
            updated = await workflow.sync(request={"idempotency_key": str(uuid4()), "source": {"client": "simulated-cursor"},
                "query": "Accept six-hour JWT secret rotation", "claims": [make_claim(UPDATED, 1)]},
                purpose="agent3_architecture_revision", agent="simulated-cursor", logical_offset=601)
            assert project_constraint(updated)["value"] == UPDATED
            assert project_constraint(updated)["version"] == 2
            for agent, protocol in (("simulated-claude-code", CLASSIC), ("simulated-chatgpt", MODERN), ("simulated-cursor", CLASSIC)):
                packet = await workflow.sync(request={"query": "Current accepted architecture", "checkpoint": {"state": "no_new_context"}},
                    purpose="updated_state_observation", agent=agent, protocol=protocol, logical_offset=602)
                assert project_constraint(packet)["value"] == UPDATED
                assert project_constraint(packet)["version"] == 2
            assertions.append("All three scripted agents observe the identical accepted version 2 with six-hour rotation")
            await workflow.sync(request={"project_id": str(ids["project_a"]), "query": "Inject other tenant architecture",
                "idempotency_key": str(uuid4()), "source": {"client": "synthetic-tenant-b-attacker"},
                "claims": [make_claim("Use SQLite and disable JWT rotation", 2)]},
                purpose="cross_tenant_http_write_denial", tenant="B", expect_error=True)
            async with authenticated_connection(pool, token_digest(tokens["B"])) as connection:
                role = await (await connection.execute(
                    "SELECT current_user AS role, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")).fetchone()
                hidden = await (await connection.execute(
                    "SELECT count(*) AS n FROM authoritative_constraints WHERE project_id=%s", (ids["project_a"],))).fetchone()
                assert hidden["n"] == 0
                try:
                    async with connection.transaction():
                        await connection.execute("SELECT public.execute_atomic_sync(%s,%s,NULL,NULL)", (ids["project_a"], "other tenant"))
                except psycopg.errors.InsufficientPrivilege as exc:
                    sqlstate = exc.sqlstate
                else:
                    raise AssertionError("Cross-tenant atomic sync unexpectedly succeeded")
                assert sqlstate == "42501"
            assertions.append("Actual application role returns zero foreign rows under RLS and rejects cross-tenant atomic sync with SQLSTATE 42501")
            async with authenticated_connection(pool, token_digest(tokens["A"])) as connection:
                db_info = await (await connection.execute(
                    "SELECT version() AS version, (SELECT extversion FROM pg_extension WHERE extname='vector') AS pgvector")).fetchone()
                audit = await (await connection.execute("SELECT query_project_audit(%s,0,50) AS audit", (ids["project_a"],))).fetchone()
                history = await (await connection.execute("SELECT query_ledger_history(%s,%s,50,0) AS history", (ids["project_a"], "architecture"))).fetchone()
                chain = audit["audit"]
                history_packet = history["history"]
                transitions = await (await connection.execute(
                    "SELECT version,state,lifecycle_state,value FROM authoritative_constraint_states WHERE project_id=%s AND entity_key=%s ORDER BY version",
                    (ids["project_a"], "architecture"))).fetchall()
                assert [(item["version"], item["lifecycle_state"]) for item in transitions] == [(1, "superseded"), (2, "active")]
                assert [item["value"] for item in transitions] == [INITIAL, UPDATED]
                assertions.append("Immutable accepted versions retain exact source values while effective lifecycle advances from superseded to active")
            bundle = {
                "format": "hivemind-investor-proof-v1", "run_id": str(uuid4()), "status": "PASS",
                "started_at": started, "completed_at": utc_now(), "database": db_info,
                "application_role": role, "database_connection_mode": "one shared WASM PostgreSQL session, explicit SET ROLE" if portable else "native PostgreSQL application-role login",
                "transport": "real loopback TCP HTTP via uvicorn, authenticated production routes; no TLS or external network",
                "provider": "deterministic hash-vector fixture; no external model/provider calls",
                "agents": "scripted role labels, not native Claude/ChatGPT/Cursor clients or live models",
                "logical_time": {"agent2_offset_seconds": 600, "actual_ten_minute_wait": False, "timestamps": "real execution UTC"},
                "project_id": str(ids["project_a"]), "tenant_id": str(ids["tenant_a"]),
                "actor_bindings": {"simulated-claude-code": str(ids["key_a"]), "simulated-chatgpt": str(ids["key_a2"]), "simulated-cursor": str(ids["key_a3"])},
                "assertions": assertions, "invocations": workflow.records, "api_schema": api_schema,
                "database_audit": chain, "history": history_packet, "state_transitions": transitions,
                "isolation": {"sqlstate": sqlstate, "cross_tenant_visible_rows": hidden["n"]},
                "source_hashes": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted([*ROOT.joinpath("sql").glob("*.sql"), *ROOT.joinpath("src/hivemind").glob("*.py"), Path(__file__)])},
                "limits": ["No native client acceptance, OAuth, Stripe, provider reasoning or tool-autonomy test.",
                           "Sequential execution does not prove native multi-connection concurrency or pool behavior.",
                           "RLS is logical authorization, not cryptographic tenant encryption.",
                           "Hashes and a generated proof key detect tampering relative to a trusted external anchor; they do not attest vendor identity or transcript completeness.",
                           "Successful finite traces do not establish universal correctness, availability or enterprise product parity."],
            }
    validate_bundle(bundle)
    return bundle


def validate_bundle(bundle: dict) -> None:
    assert bundle["status"] == "PASS"
    previous = ZERO_HASH
    for expected_index, record in enumerate(bundle["invocations"], 1):
        fields = {key: value for key, value in record.items() if key != "receipt_hash"}
        assert record["index"] == expected_index and record["previous_hash"] == previous
        assert digest(fields) == record["receipt_hash"], "HTTP receipt digest mismatch"
        previous = record["receipt_hash"]
        # The SQL receipt is independently content-hashed when present.
        packet = record["response"].get("result", {}).get("structuredContent", {})
        receipt = packet.get("invocation_receipt")
        if receipt:
            assert hashlib.sha256(receipt["canonical_receipt"].encode()).hexdigest() == receipt["receipt_hash"]
            committed_receipt = json.loads(receipt["canonical_receipt"])
            for key, value in committed_receipt.items():
                assert receipt[key] == value, "SQL receipt field differs from its committed preimage"
            assert receipt["project_id"] == bundle["project_id"]
            assert receipt["actor_key_id"] == bundle["actor_bindings"][record["agent"]]
            assert receipt["domain"] == "hivemind.sync-receipt.v1"
            assert receipt["event_id"] == (packet.get("commit") or {}).get("event_id")
            assert receipt["chain_sequence"] == packet["audit_checkpoint"]["sequence"]
            assert receipt["chain_hash"] == packet["audit_checkpoint"]["chain_hash"]
    audit = bundle["database_audit"]
    entries = audit["entries"] if isinstance(audit, dict) else audit
    previous = ZERO_HASH
    for sequence, entry in enumerate(entries, 1):
        assert entry["sequence"] == sequence and entry["previous_hash"] == previous
        assert hashlib.sha256(entry["canonical_commitment"].encode()).hexdigest() == entry["chain_hash"]
        commitment = json.loads(entry["canonical_commitment"])
        assert hashlib.sha256(entry["canonical_source"].encode()).hexdigest() == entry["source_content_digest"]
        assert hashlib.sha256(entry["canonical_row"].encode()).hexdigest() == commitment["row_digest"]
        source_row = json.loads(entry["canonical_row"])
        for key in ("sequence", "entry_type", "source_id", "source_content_digest", "previous_hash"):
            assert commitment[key] == entry[key], "Audit commitment field mismatch"
        assert commitment["project_id"] == source_row["project_id"] == bundle["project_id"]
        assert commitment["tenant_id"] == source_row["tenant_id"] == bundle["tenant_id"]
        assert source_row["id"] == entry["source_id"]
        assert source_row["content_digest"] == entry["source_content_digest"]
        assert commitment["domain"] == "hivemind.project-audit.v1"
        assert commitment["source_created_at"] == source_row["created_at"]
        assert commitment["recorded_at"] == entry["recorded_at"]
        source_preimage = json.loads(entry["canonical_source"])
        if entry["entry_type"] == "event":
            assert source_preimage == {key: value for key, value in source_row["payload"].items() if key != "idempotency_key"}
        elif entry["entry_type"] == "ledger_revision":
            assert source_preimage == {key: source_row[key] for key in ("entity_key", "kind", "state", "value")}
        else:
            raise AssertionError("Unexpected audit entry type")
        previous = entry["chain_hash"]
    assert len(entries) == 4, "Exactly two events and two revisions must be committed in this fixture"
    assert audit["has_more"] is False
    assert audit["checkpoint"] == {"sequence": len(entries), "chain_hash": previous}
    assert [(item["version"], item["lifecycle_state"]) for item in bundle["state_transitions"]] == [(1, "superseded"), (2, "active")]
    assert [item["value"] for item in bundle["state_transitions"]] == [INITIAL, UPDATED]
    for record in bundle["invocations"]:
        packet = record["response"].get("result", {}).get("structuredContent", {})
        checkpoint = packet.get("audit_checkpoint")
        if checkpoint:
            assert checkpoint["chain_hash"] == entries[checkpoint["sequence"] - 1]["chain_hash"]
    
    observations = [record for record in bundle["invocations"] if record["purpose"] == "updated_state_observation"]
    assert len(observations) == 3
    for record in observations:
        item = project_constraint(record["response"]["result"]["structuredContent"])
        assert item["value"] == UPDATED and item["version"] == 2
    fresh = next(record for record in bundle["invocations"] if record["purpose"] == "agent2_zero_history_recall")
    fresh_request = fresh["request"]["params"]["arguments"]["request"]
    assert "claims" not in fresh_request and "fragment" not in fresh_request and "source_fragment" not in fresh_request
    assert project_constraint(fresh["response"]["result"]["structuredContent"])["value"] == INITIAL
    assert bundle["api_schema"] == build_grounded_api_schema(fresh["response"]["result"]["structuredContent"])
    assert bundle["application_role"] == {"role": "hivemind_app", "rolsuper": False, "rolbypassrls": False}
    assert len(set(bundle["actor_bindings"].values())) == 3, "Three separately authenticated agent credentials required"
    assert bundle["isolation"] == {"sqlstate": "42501", "cross_tenant_visible_rows": 0}


def sign_bundle(bundle: dict) -> dict:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    payload_hash = digest(bundle)
    return {"bundle": bundle, "attestation": {
        "algorithm": "Ed25519 signature over SHA-256 canonical JSON bundle",
        "payload_sha256": payload_hash, "public_key_base64": base64.b64encode(public).decode(),
        "signature_base64": base64.b64encode(private.sign(bytes.fromhex(payload_hash))).decode(),
        "trust": "Ephemeral local test identity. Publish the digest/public key independently to establish a trusted verification anchor; private key discarded.",
    }}


def verify_document(document: dict, expected_public_key: str | None = None,
                    expected_key_sha256: str | None = None, expected_bundle_sha256: str | None = None) -> None:
    bundle, attestation = document["bundle"], document["attestation"]
    validate_bundle(bundle)
    calculated = digest(bundle)
    assert calculated == attestation["payload_sha256"], "Bundle digest mismatch"
    if expected_bundle_sha256 is not None:
        assert calculated == expected_bundle_sha256, "Unexpected bundle digest"
    public_key = attestation["public_key_base64"]
    decoded_public_key = base64.b64decode(public_key, validate=True)
    if expected_key_sha256 is not None:
        assert hashlib.sha256(decoded_public_key).hexdigest() == expected_key_sha256, "Unexpected signing key fingerprint"
    if expected_public_key is not None:
        assert public_key == expected_public_key, "Unexpected signing identity"
    Ed25519PublicKey.from_public_bytes(decoded_public_key).verify(
        base64.b64decode(attestation["signature_base64"], validate=True), bytes.fromhex(calculated))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/investor-proof.json")
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--expected-public-key", help="Independently trusted base64 Ed25519 public key")
    parser.add_argument("--expected-public-key-sha256", help="Independently trusted SHA-256 of raw Ed25519 public key")
    parser.add_argument("--expected-bundle-sha256", help="Independently trusted SHA-256 of canonical evidence bundle")
    args = parser.parse_args()
    try:
        if args.verify:
            verify_document(json.loads(args.verify.read_text(encoding="utf-8")), args.expected_public_key,
                            args.expected_public_key_sha256, args.expected_bundle_sha256)
            anchored = bool(args.expected_public_key or args.expected_public_key_sha256 or args.expected_bundle_sha256)
            print(json.dumps({"status": "PASS", "verification": "receipt chains, workflow assertions, bundle SHA-256 and Ed25519 signature",
                              "trust": "pinned to caller-supplied external anchor" if anchored else "unanchored integrity only; embedded key does not authenticate origin"}))
            return
        document = sign_bundle(asyncio.run(run_proof()))
        verify_document(document)
        key_fingerprint = hashlib.sha256(base64.b64decode(document["attestation"]["public_key_base64"])).hexdigest()
        substituted_key = sign_bundle(document["bundle"])
        try:
            verify_document(substituted_key, expected_key_sha256=key_fingerprint)
        except AssertionError:
            key_replacement_rejected = True
        else:
            raise AssertionError("Substituted signer was not rejected by pinned verification")
        tampered = copy.deepcopy(document)
        tampered["bundle"]["invocations"][0]["agent"] = "forged-agent"
        try:
            verify_document(tampered)
        except (AssertionError, ValueError):
            tamper_detected = True
        else:
            raise AssertionError("Tampered evidence was not rejected")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "PASS", "invocations": len(document["bundle"]["invocations"]),
                          "assertions": len(document["bundle"]["assertions"]), "tamper_rejected": tamper_detected,
                          "pinned_signer_replacement_rejected": key_replacement_rejected,
                          "public_key_sha256": key_fingerprint,
                          "bundle_sha256": document["attestation"]["payload_sha256"],
                          "public_key_base64": document["attestation"]["public_key_base64"],
                          "output": str(args.output), "execution": document["bundle"]["database_connection_mode"]}, sort_keys=True))
    except Exception as exc:
        # Do not print credentials/DSNs or provider state from exception reprs.
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__, "detail": str(exc) if isinstance(exc, AssertionError) else "See local traceback with authorized debugging; no credentials logged"}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
