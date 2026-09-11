import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastmcp.server.auth import AccessToken, TokenVerifier

from hivemind.app import create_app
from hivemind.config import Settings

TOKEN = "hvm_" + "t" * 43
KEY_HASH = "a" * 64


class FakeVerifier(TokenVerifier):
    async def verify_token(self, token):
        if token != TOKEN:
            return None
        return AccessToken(token=token, client_id="test-key", scopes=["memory:access"],
                           claims={"key_hash": KEY_HASH})


class FakeCursor:
    async def fetchone(self):
        return {"healthy": 1}


class FakeConnection:
    async def execute(self, query):
        assert query == "SELECT 1 AS healthy"
        return FakeCursor()


class FakePool:
    async def open(self, **kwargs):
        pass

    async def close(self):
        pass

    @asynccontextmanager
    async def connection(self, **kwargs):
        yield FakeConnection()


class FakeEngine:
    def __init__(self):
        self.commits = []

    async def sync(self, request, key_hash):
        assert key_hash == KEY_HASH
        if request.claims or request.fragment:
            self.commits.append(request)
        return {"event_id": str(uuid4()), "replayed": False, "embedding_status": "queued"}

    async def manage(self, request, key_hash):
        assert key_hash == KEY_HASH
        return {"project_id": str(request.project_id), "constraints": [], "memories": []}


@pytest.fixture
async def client():
    engine = FakeEngine()
    app = create_app(Settings(), pool=FakePool(), engine=engine, auth=FakeVerifier())
    async with LifespanManager(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            yield c, engine


def rpc_response(response):
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise AssertionError("No JSON-RPC response in SSE stream")
    return response.json()


def headers(**extra):
    return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25", **extra}


async def rpc(client, method, params=None, **extra):
    return await client.post("/mcp/v1", headers=headers(**extra),
                             json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


async def test_health_has_no_secrets_and_uses_exact_route(client):
    c, _ = client
    response = await c.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert "x-request-id" in response.headers


async def test_unauthenticated_and_query_token_requests_are_rejected(client):
    c, _ = client
    response = await c.post("/mcp/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    assert "www-authenticate" in response.headers
    response = await c.post("/mcp/v1?token=" + TOKEN, headers=headers(), json={})
    assert response.status_code == 400
    assert TOKEN not in response.text


async def test_origin_validation_does_not_require_origin_for_native_clients(client):
    c, _ = client
    response = await rpc(c, "tools/list", Origin="https://evil.example")
    assert response.status_code == 403
    response = await rpc(c, "tools/list")
    assert response.status_code == 200


async def test_initialize_and_exactly_two_tools_over_streamable_http(client):
    c, _ = client
    response = await rpc(c, "initialize", {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "hivemind-test", "version": "1.0"},
    })
    assert response.status_code == 200
    assert rpc_response(response)["result"]["serverInfo"]["name"] == "Hivemind Scale"
    listed = rpc_response(await rpc(c, "tools/list"))["result"]["tools"]
    assert {tool["name"] for tool in listed} == {"sync_context", "manage_ledger"}
    annotations = {tool["name"]: tool["annotations"] for tool in listed}
    assert annotations["sync_context"]["readOnlyHint"] is False
    assert annotations["manage_ledger"]["idempotentHint"] is True


async def test_tool_call_reaches_scoped_engine_and_returns_structured_result(client):
    c, engine = client
    payload = {"project_id": str(uuid4()), "idempotency_key": str(uuid4()),
               "source": {"client": "cursor", "conversation_id": "thread-1"},
               "claims": [{"entity_key": "database", "kind": "constraint", "state": "accepted", "value": "PostgreSQL 17", "expected_version": 0, "evidence": "Use PostgreSQL 17."}]}
    response = await rpc(c, "tools/call", {"name": "sync_context", "arguments": {"request": payload}})
    body = rpc_response(response)
    assert body["result"].get("isError", False) is False, body
    assert len(engine.commits) == 1
    assert body["result"]["structuredContent"]["embedding_status"] == "queued"


async def test_extra_tenant_field_cannot_override_authenticated_identity(client):
    c, engine = client
    payload = {"project_id": str(uuid4()), "tenant_id": str(uuid4()),
               "idempotency_key": str(uuid4()), "source": {"client": "cursor", "conversation_id": "x"},
               "fragment": "data"}
    response = await rpc(c, "tools/call", {"name": "sync_context", "arguments": {"request": payload}})
    body = rpc_response(response)
    assert body.get("error") or body.get("result", {}).get("isError")
    assert not engine.commits


async def test_chunked_oversized_body_is_rejected(client):
    c, _ = client
    async def chunks():
        yield b" " * 70_000
        yield b" " * 70_000
    response = await c.post("/mcp/v1", headers=headers(), content=chunks())
    assert response.status_code == 413


async def test_initialize_transmits_real_instructions_field(client):
    from hivemind.server import SERVER_INSTRUCTIONS
    c, _ = client
    response = await rpc(c, "initialize", {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "hivemind-test", "version": "1.0"},
    })
    result = rpc_response(response)["result"]
    assert result["instructions"] == SERVER_INSTRUCTIONS
    assert "serverInstructions" not in result
    assert "host" in result["instructions"] and "untrusted" in result["instructions"]


async def test_dynamic_descriptors_do_not_mislabel_write_capability(client):
    c, _ = client
    for _ in range(2):
        tools = rpc_response(await rpc(c, "tools/list"))["result"]["tools"]
        assert len(tools) == 2
        for tool in tools:
            assert tool["annotations"]["readOnlyHint"] is False
            assert tool["annotations"]["destructiveHint"] is True
            assert tool["_meta"]["hivemind.dev/credential_present"] is True
            assert KEY_HASH not in json.dumps(tool)


async def test_recall_needs_no_project_id_or_mutation_fields(client):
    c, engine = client
    result = rpc_response(await rpc(c, "tools/call", {
        "name": "sync_context", "arguments": {"request": {"query": "Current architecture"}}
    }))["result"]
    assert not result.get("isError")
    assert not engine.commits
    assert result["content"][0]["annotations"]["audience"] == ["assistant"]


async def test_body_deadline_applies_before_authentication(monkeypatch):
    import asyncio

    import hivemind.app as application
    sent = []

    async def downstream(scope, receive, send):
        raise AssertionError("Timed-out request must never reach authentication or handlers")

    async def receive():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"x", "more_body": True}

    async def send(message):
        sent.append(message)

    monkeypatch.setattr(application, "REQUEST_BODY_TIMEOUT", 0.01)
    guard = application.RequestGuard(downstream, Settings())
    await guard({"type": "http", "path": "/mcp/v1", "method": "POST", "headers": []}, receive, send)
    assert sent[0]["status"] == 408


async def test_many_tiny_body_chunks_are_coalesced():
    from hivemind.app import RequestGuard
    observed = []
    count = 0

    async def receive():
        nonlocal count
        count += 1
        return {"type": "http.request", "body": b"a", "more_body": count < 5000}

    async def send(message):
        observed.append(message)

    async def downstream(scope, receive, send):
        message = await receive()
        assert message == {"type": "http.request", "body": b"a" * 5000, "more_body": False}
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    await RequestGuard(downstream, Settings())(
        {"type": "http", "path": "/token", "method": "POST", "headers": []}, receive, send,
    )
    assert count == 5000
    assert observed[0]["status"] == 200
