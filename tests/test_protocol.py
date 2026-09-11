"""Executed ASGI wire tests of the supported dual-era protocol surface."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.applications import Starlette
from starlette.routing import Mount

from hivemind.protocol import CAPABILITIES_KEY, MODERN_VERSION, VERSION_KEY, DualProtocolRouter
from hivemind.server import SERVER_INSTRUCTIONS, create_mcp

TOKEN = "hvm_" + "p" * 43
KEY_HASH = "c" * 64


class Verifier(TokenVerifier):
    async def verify_token(self, token):
        if token not in {TOKEN, "missing-scope", "expired"}:
            return None
        return AccessToken(token=token, client_id="protocol-test", claims={"key_hash": KEY_HASH},
                           scopes=[] if token == "missing-scope" else ["memory:access"],
                           expires_at=1 if token == "expired" else None)


class Engine:
    def __init__(self):
        self.calls = []

    async def sync(self, request, key_hash):
        self.calls.append((request, key_hash))
        return {"constraints": [], "key_verified": key_hash == KEY_HASH}

    async def manage(self, request, key_hash):
        self.calls.append((request, key_hash))
        return {"revisions": []}


@pytest.fixture
async def wire_client():
    engine = Engine()
    auth = Verifier(required_scopes=["memory:access"])
    mcp = create_mcp(engine, auth)
    classic = mcp.http_app(path="/mcp/v1", stateless_http=True, json_response=True)
    router = DualProtocolRouter(classic_app=classic, mcp=mcp, auth=auth,
                                instructions=SERVER_INSTRUCTIONS,
                                resource_url="https://memory.example/mcp/v1",
                                allowed_origins={"https://trusted.example"})

    @asynccontextmanager
    async def lifespan(app):
        async with classic.lifespan(app):
            yield

    app = Starlette(routes=[Mount("/", app=router)], lifespan=lifespan)
    engine.router = router
    engine.app = app
    async with LifespanManager(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as client:
            yield client, engine


def modern(method="server/discover", **params):
    body = {"jsonrpc": "2.0", "id": 7, "method": method, "params": {"_meta": {
        VERSION_KEY: MODERN_VERSION, CAPABILITIES_KEY: {},
    }, **params}}
    headers = {"authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream",
               "content-type": "application/json", "mcp-protocol-version": MODERN_VERSION,
               "mcp-method": method}
    if method in {"tools/call", "prompts/get", "resources/read"}:
        headers["mcp-name"] = params.get("name", params.get("uri", "unknown"))
    return body, headers


async def post(client, body, headers):
    return await client.post("/mcp/v1", json=body, headers=headers)


async def test_classic_initialize_stays_on_sdk_and_preserves_instructions(wire_client):
    client, _ = wire_client
    response = await client.post("/mcp/v1", json={"jsonrpc": "2.0", "id": 1,
        "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "classic-test", "version": "1"}}},
        headers={"authorization": f"Bearer {TOKEN}", "accept": "application/json,text/event-stream"})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["instructions"] == SERVER_INSTRUCTIONS
    assert "resultType" not in result


async def test_discovery_is_stateless_private_and_advertises_only_tools(wire_client):
    client, _ = wire_client
    response = await post(client, *modern())
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    assert result["capabilities"] == {"tools": {}}
    assert {"2026-07-28", "2025-11-25"} <= set(result["supportedVersions"])
    assert result["ttlMs"] == 0 and result["cacheScope"] == "private"
    assert result["instructions"] == SERVER_INSTRUCTIONS
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "Hivemind Scale"
    assert "mcp-session-id" not in response.headers
    assert response.headers["cache-control"] == "no-store"


async def test_tool_list_uses_registered_schema_and_truthful_annotations(wire_client):
    client, _ = wire_client
    response = await post(client, *modern("tools/list"))
    result = response.json()["result"]
    assert result["resultType"] == "complete" and result["ttlMs"] == 0
    assert result["cacheScope"] == "private"
    assert {t["name"] for t in result["tools"]} == {"sync_context", "manage_ledger"}
    for tool in result["tools"]:
        assert tool["annotations"]["readOnlyHint"] is False
        assert tool["_meta"]["hivemind.dev/credential_present"] is True
        assert "request" in tool["inputSchema"]["properties"]
        assert "ttlMs" not in tool and "execution" not in tool
        assert KEY_HASH not in json.dumps(tool)


async def test_modern_tool_calls_same_authenticated_engine_without_initialize(wire_client):
    client, engine = wire_client
    body, headers = modern("tools/call", name="sync_context", arguments={"request": {
        "query": "Architecture", "checkpoint": {"state": "no_new_context"}}})
    response = await post(client, body, headers)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result["isError"] and result["resultType"] == "complete"
    assert result["structuredContent"]["key_verified"] is True
    assert len(engine.calls) == 1 and engine.calls[0][1] == KEY_HASH


@pytest.mark.parametrize("token,status", [(None, 401), ("wrong", 401), ("expired", 401), ("missing-scope", 403)])
async def test_modern_authentication_and_scope_are_identical_to_sdk(wire_client, token, status):
    client, engine = wire_client
    body, headers = modern()
    if token is None:
        del headers["authorization"]
    else:
        headers["authorization"] = f"Bearer {token}"
    response = await post(client, body, headers)
    assert response.status_code == status
    assert "www-authenticate" in response.headers
    assert "https://memory.example/.well-known/oauth-protected-resource/mcp/v1" in response.headers["www-authenticate"]
    assert not engine.calls
    if token is None:
        assert "error=" not in response.headers["www-authenticate"]


@pytest.mark.parametrize("header,value", [("mcp-protocol-version", None),
    ("mcp-protocol-version", "2025-11-25"), ("mcp-method", None),
    ("mcp-method", "tools/call"), ("mcp-name", "unexpected")])
async def test_header_body_mismatch_is_modern_error_not_downgrade(wire_client, header, value):
    client, _ = wire_client
    body, headers = modern()
    if value is None:
        headers.pop(header, None)
    else:
        headers[header] = value
    response = await post(client, body, headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


async def test_unknown_version_advertises_supported_versions(wire_client):
    client, _ = wire_client
    body, headers = modern()
    body["params"]["_meta"][VERSION_KEY] = headers["mcp-protocol-version"] = "2099-01-01"
    response = await post(client, body, headers)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == -32022 and error["data"]["requested"] == "2099-01-01"
    assert MODERN_VERSION in error["data"]["supported"]


@pytest.mark.parametrize("method", ["initialize", "ping", "resources/list", "subscriptions/listen", "tasks/get"])
async def test_unimplemented_modern_methods_do_not_claim_capability(wire_client, method):
    client, _ = wire_client
    response = await post(client, *modern(method))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32601


@pytest.mark.parametrize("mutation", ["missing_meta", "missing_capabilities", "capabilities_bool", "roots_bool", "sampling_tools_bool", "client_info_invalid"])
async def test_request_metadata_is_validated_on_every_request(wire_client, mutation):
    client, _ = wire_client
    body, headers = modern()
    meta = body["params"]["_meta"]
    if mutation == "missing_meta":
        del body["params"]["_meta"]
    elif mutation == "missing_capabilities":
        del meta[CAPABILITIES_KEY]
    elif mutation == "capabilities_bool":
        meta[CAPABILITIES_KEY] = True
    elif mutation == "roots_bool":
        meta[CAPABILITIES_KEY] = {"roots": True}
    elif mutation == "sampling_tools_bool":
        meta[CAPABILITIES_KEY] = {"sampling": {"tools": True}}
    else:
        meta["io.modelcontextprotocol/clientInfo"] = {"name": "spoof"}
    response = await post(client, body, headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


async def test_unknown_optional_capabilities_fall_back_to_core(wire_client):
    client, _ = wire_client
    body, headers = modern()
    body["params"]["_meta"][CAPABILITIES_KEY] = {"extensions": {"com.vendor/unknown": {}}}
    response = await post(client, body, headers)
    assert response.status_code == 200
    assert "extensions" not in response.json()["result"]["capabilities"]


@pytest.mark.parametrize("encoding", ["plain", "base64"])
async def test_tool_name_mirroring_accepts_both_valid_encodings(wire_client, encoding):
    client, _ = wire_client
    body, headers = modern("tools/call", name="sync_context", arguments={"request": {
        "query": "Architecture", "checkpoint": {"state": "no_new_context"}}})
    if encoding == "base64":
        headers["mcp-name"] = "=?base64?" + base64.b64encode(b"sync_context").decode() + "?="
    response = await post(client, body, headers)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("name_header", [None, "manage_ledger", "=?base64?invalid!*=??="])
async def test_missing_mismatched_or_invalid_name_header_blocks_execution(wire_client, name_header):
    client, engine = wire_client
    body, headers = modern("tools/call", name="sync_context", arguments={"request": {}})
    if name_header is None:
        del headers["mcp-name"]
    else:
        headers["mcp-name"] = name_header
    response = await post(client, body, headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert not engine.calls


@pytest.mark.parametrize("body", [[], {"jsonrpc": "2.0", "id": None, "method": "server/discover"},
    {"jsonrpc": "2.0", "id": True, "method": "server/discover"},
    {"jsonrpc": "2.0", "id": 1, "result": {}}])
async def test_batches_notifications_invalid_ids_and_responses_are_rejected(wire_client, body):
    client, _ = wire_client
    _, headers = modern()
    response = await post(client, body, headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600


@pytest.mark.parametrize("raw", [b'{"id":1,"id":2}', b'{"x":NaN}', b'\xff', b'{'])
async def test_duplicate_members_invalid_utf8_and_nonfinite_json_are_rejected(wire_client, raw):
    client, engine = wire_client
    _, headers = modern()
    response = await client.post("/mcp/v1", content=raw, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700
    assert not engine.calls


async def test_duplicate_authorization_header_is_never_ambiguous(wire_client):
    client, engine = wire_client
    body, headers = modern()
    duplicated = list(headers.items()) + [("Authorization", "Bearer second")]
    response = await client.post("/mcp/v1", json=body, headers=duplicated)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert not engine.calls


@pytest.mark.parametrize("method", ["GET", "DELETE", "PATCH"])
async def test_modern_endpoint_only_accepts_post(wire_client, method):
    client, _ = wire_client
    _, headers = modern()
    response = await client.request(method, "/mcp/v1", headers=headers)
    assert response.status_code == 405 and response.headers["allow"] == "POST"


async def test_legacy_session_and_resumption_headers_do_not_create_modern_state(wire_client):
    client, _ = wire_client
    body, headers = modern()
    headers.update({"mcp-session-id": "untrusted-session", "last-event-id": "untrusted-event"})
    response = await post(client, body, headers)
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers and "last-event-id" not in response.headers


async def test_origin_and_query_credentials_are_rejected(wire_client):
    client, _ = wire_client
    body, headers = modern()
    denied = await post(client, body, {**headers, "origin": "https://evil.example"})
    assert denied.status_code == 403
    allowed = await post(client, body, {**headers, "origin": "https://trusted.example"})
    assert allowed.status_code == 200
    query = await client.post("/mcp/v1?token=secret", json=body, headers=headers)
    assert query.status_code == 400 and "secret" not in query.text


@pytest.mark.parametrize("header,value,status", [("accept", "application/json", 406),
    ("content-type", "text/plain", 415)])
async def test_http_media_contract_is_enforced(wire_client, header, value, status):
    client, _ = wire_client
    body, headers = modern()
    headers[header] = value
    assert (await post(client, body, headers)).status_code == status


@pytest.mark.parametrize("arguments", [{"request": {}, "tenant_id": "spoof"},
    {"request": {"tenant_id": "spoof"}}, {"request": {"query": True}}])
async def test_invalid_tool_arguments_do_not_reach_engine_or_echo_inputs(wire_client, arguments, caplog):
    client, engine = wire_client
    response = await post(client, *modern("tools/call", name="sync_context", arguments=arguments))
    assert response.status_code == 400 and response.json()["error"]["code"] == -32602
    assert not engine.calls
    assert "spoof" not in response.text and "spoof" not in caplog.text


@pytest.mark.parametrize("arguments", [{}, {"request": {}}, {"request": None}])
async def test_empty_sync_arguments_reach_shared_checkpoint_without_schema_drift(wire_client, arguments):
    client, engine = wire_client
    response = await post(client, *modern("tools/call", name="sync_context", arguments=arguments))
    assert response.status_code == 200
    assert not response.json()["result"]["isError"]
    assert len(engine.calls) == 1


async def test_cursor_and_task_extension_parameters_are_explicit_errors(wire_client):
    client, engine = wire_client
    response = await post(client, *modern("tools/list", cursor="invented"))
    assert response.status_code == 400 and response.json()["error"]["code"] == -32602
    response = await post(client, *modern("tools/call", name="sync_context", arguments={"request": {}}, task={"ttl": 1}))
    assert response.status_code == 400 and not engine.calls


async def test_oversized_chunked_body_is_bounded_before_auth(wire_client):
    client, engine = wire_client
    _, headers = modern()

    async def chunks():
        yield b"x" * 70_000
        yield b"x" * 70_000

    response = await client.post("/mcp/v1", content=chunks(), headers=headers)
    assert response.status_code == 413 and not engine.calls


async def test_disconnect_cancels_inflight_execution_without_sending_response(wire_client, monkeypatch):
    _, engine = wire_client
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending_sync(request, key_hash):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(engine, "sync", pending_sync)
    body, headers = modern("tools/call", name="sync_context", arguments={})
    scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "https",
             "path": "/mcp/v1", "raw_path": b"/mcp/v1", "query_string": b"",
             "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
             "server": ("memory.example", 443), "client": ("127.0.0.1", 4000), "app": engine.app}
    delivered = False
    sent = []

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        await entered.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async with asyncio.timeout(2):
        await engine.router(scope, receive, send)
    assert cancelled.is_set()
    assert sent == []


async def test_modern_execution_deadline_cancels_work_and_returns_retry_error(wire_client, monkeypatch):
    client, engine = wire_client
    cancelled = asyncio.Event()

    async def pending_sync(request, key_hash):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(engine, "sync", pending_sync)
    engine.router.request_timeout = 0.01
    response = await post(client, *modern("tools/call", name="sync_context", arguments={}))
    assert response.status_code == 504
    assert response.json()["error"]["code"] == -32603
    assert cancelled.is_set()


async def test_split_accept_headers_are_combined_but_zero_quality_is_rejected(wire_client):
    client, _ = wire_client
    body, headers = modern()
    headers["accept"] = "application/json"
    combined = list(headers.items()) + [("Accept", "text/event-stream")]
    assert (await client.post("/mcp/v1", json=body, headers=combined)).status_code == 200
    headers["accept"] = "application/json;q=0,text/event-stream"
    assert (await post(client, body, headers)).status_code == 406
