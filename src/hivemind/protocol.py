"""Dual-era MCP endpoint: pinned SDK classic transport plus the 2026 tools core.

The adapter implements 2026-07-28 discovery, tool listing and calls. It advertises
no subscriptions, tasks, resources, prompts, sampling or elicitation. Authentication,
registered tool functions and domain transactions are shared with the classic SDK.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections import Counter
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.exceptions import ValidationError as ToolValidationError
from fastmcp.server.auth.middleware import RequireAuthMiddleware
from fastmcp.server.http import RequestContextMiddleware
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS as SDK_VERSIONS
from pydantic import AnyHttpUrl, ValidationError
from starlette.responses import JSONResponse, Response

from hivemind import __version__
from hivemind.ledger import ManageRequest, SyncRequest

MODERN_VERSION = "2026-07-28"
CLASSIC_VERSIONS = tuple(SDK_VERSIONS)
SUPPORTED_VERSIONS = (MODERN_VERSION, *reversed(CLASSIC_VERSIONS))
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
INFO_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
SUPPORTED_METHODS = frozenset({"server/discover", "tools/list", "tools/call"})
TOOL_MODELS = {"sync_context": SyncRequest, "manage_ledger": ManageRequest}
_SINGLE_HEADERS = frozenset({
    b"authorization", b"origin", b"host", b"content-type", b"content-length",
    b"mcp-protocol-version", b"mcp-method", b"mcp-name",
})


class ProtocolFault(Exception):
    """A deliberately non-sensitive, fully specified wire failure."""

    def __init__(self, code: int, message: str, status: int = 400, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.data = data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object member")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _request_id(message):
    if not isinstance(message, dict):
        return None
    value = message.get("id")
    if isinstance(value, str) and len(value.encode("utf-8", errors="replace")) <= 256:
        return value
    if type(value) is int and -(2**53 - 1) <= value <= 2**53 - 1:
        return value
    return None


def _decode_header(value: bytes, *, encoded: bool = False) -> str:
    try:
        text = value.decode("ascii")
        if not text or text.strip() != text or any(ord(c) < 32 or ord(c) > 126 for c in text):
            raise ValueError("invalid field value")
        if encoded and text.startswith("=?base64?") and text.endswith("?="):
            return base64.b64decode(text[9:-2], validate=True).decode("utf-8")
        return text
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ProtocolFault(-32020, "Missing, malformed or inconsistent routing headers") from exc


def _metadata_url(resource_url: str | None):
    if resource_url is None:
        return None
    parsed = urlsplit(resource_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("resource_url must be an absolute HTTP resource URL")
    return AnyHttpUrl(urlunsplit((parsed.scheme, parsed.netloc,
                                 "/.well-known/oauth-protected-resource" + parsed.path, "", "")))


class DualProtocolRouter:
    """ASGI router for one endpoint; keep ``classic_app.lifespan`` in its owner.

    Construct after ``mcp.http_app`` so OAuth resource registration is complete.
    ``auth`` must be the same FastMCP auth provider as the classic app. The provider's
    public middleware authenticates every modern request, with identical scope checks.
    The owning app retains trusted-host, request logging and operational routes.
    """

    def __init__(self, *, classic_app, mcp: FastMCP, auth, instructions: str,
                 resource_url: str | None = None, endpoint: str = "/mcp/v1",
                 allowed_origins=(), max_request_bytes: int = 131072,
                 request_timeout: float = 30.0):
        if auth is None:
            raise ValueError("Dual protocol endpoint requires an authentication provider")
        if max_request_bytes <= 0 or request_timeout <= 0:
            raise ValueError("Request budgets must be positive")
        self.classic_app = classic_app
        self.mcp = mcp
        self.instructions = instructions
        self.endpoint = endpoint
        self.allowed_origins = frozenset(allowed_origins)
        self.max_request_bytes = max_request_bytes
        self.request_timeout = request_timeout
        modern = RequireAuthMiddleware(
            RequestContextMiddleware(self._handle_modern),
            required_scopes=list(auth.required_scopes or ["memory:access"]),
            resource_metadata_url=_metadata_url(resource_url),
        )
        for middleware in reversed(auth.get_middleware()):
            modern = middleware.cls(modern, *middleware.args, **middleware.kwargs)
        self.modern_app = modern

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != self.endpoint:
            return await self.classic_app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        accept_values = [value for key, value in scope.get("headers", []) if key.lower() == b"accept"]
        if accept_values:
            headers[b"accept"] = b",".join(accept_values)
        counts = Counter(key.lower() for key, _ in scope.get("headers", []))
        if any(counts[key] > 1 for key in _SINGLE_HEADERS):
            return await self._error(ProtocolFault(-32020, "Duplicate security or routing header"),
                                     None, scope, receive, send)
        origin = headers.get(b"origin")
        if origin is not None and origin.decode("utf-8", errors="replace") not in self.allowed_origins:
            return await self._error(ProtocolFault(-32600, "Origin denied", 403),
                                     None, scope, receive, send)
        query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="ignore"))
        if "token" in query or "access_token" in query:
            return await self._error(ProtocolFault(-32600, "Use an Authorization bearer header"),
                                     None, scope, receive, send)
        version_header = headers.get(b"mcp-protocol-version", b"").decode("ascii", errors="replace")
        modern_header = bool(version_header and version_header not in CLASSIC_VERSIONS)
        if scope["method"] != "POST":
            if modern_header:
                return await Response(status_code=405, headers={"Allow": "POST", "Cache-Control": "no-store"})(scope, receive, send)
            return await self.classic_app(scope, receive, send)
        body = bytearray()
        try:
            async with asyncio.timeout(15):
                while True:
                    chunk = await receive()
                    if chunk["type"] == "http.disconnect":
                        return
                    content = chunk.get("body", b"")
                    if len(body) + len(content) > self.max_request_bytes:
                        raise ProtocolFault(-32600, "Request body exceeds byte budget", 413)
                    body.extend(content)
                    if not chunk.get("more_body", False):
                        break
        except TimeoutError:
            return await self._error(ProtocolFault(-32600, "Request body deadline exceeded", 408),
                                     None, scope, receive, send)
        except ProtocolFault as exc:
            return await self._error(exc, None, scope, receive, send)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        try:
            message = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                                 parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            return await self._error(ProtocolFault(-32700, "Invalid UTF-8 JSON or duplicate member"),
                                     None, scope, replay, send)
        params = message.get("params", {}) if isinstance(message, dict) else {}
        meta = params.get("_meta", {}) if isinstance(params, dict) else {}
        modern_body = (isinstance(message, dict) and message.get("method") == "server/discover") or (
            isinstance(meta, dict) and (VERSION_KEY in meta or CAPABILITIES_KEY in meta))
        if not modern_header and not modern_body:
            # Even in the classic era, do not tolerate conflicting optional routing hints.
            try:
                self._validate_optional_routing(headers, message)
            except ProtocolFault as exc:
                return await self._error(exc, _request_id(message), scope, replay, send)
            return await self.classic_app(scope, replay, send)
        modern_scope = dict(scope)
        modern_scope["hivemind.modern_message"] = message
        modern_scope["hivemind.routing_headers"] = headers
        response_started = False

        async def modern_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.modern_app(modern_scope, replay, modern_send)
        except Exception:
            # Auth provider outages and unforeseen implementation faults never echo input.
            if response_started:
                raise
            await self._error(ProtocolFault(-32603, "Service unavailable", 503),
                              _request_id(message), scope, replay, send)

    @staticmethod
    def _validate_optional_routing(headers, message):
        if not isinstance(message, dict):
            return
        if b"mcp-method" in headers and _decode_header(headers[b"mcp-method"]) != message.get("method"):
            raise ProtocolFault(-32020, "Missing, malformed or inconsistent routing headers")
        if b"mcp-name" in headers:
            params = message.get("params", {})
            name = params.get("name", params.get("uri")) if isinstance(params, dict) else None
            if _decode_header(headers[b"mcp-name"], encoded=True) != name:
                raise ProtocolFault(-32020, "Missing, malformed or inconsistent routing headers")

    async def _handle_modern(self, scope, receive, send):
        message = scope["hivemind.modern_message"]
        request_id = _request_id(message)
        try:
            method, params = self._validate_modern(message, scope["hivemind.routing_headers"])
            async with asyncio.timeout(self.request_timeout):
                result = await self._execute_until_disconnect(method, params, receive)
            if result is None:
                return
            result["resultType"] = "complete"
            result["_meta"] = {**result.get("_meta", {}), SERVER_INFO_KEY: {
                "name": "Hivemind Scale", "version": __version__,
            }}
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode()) > 262144:
                raise ProtocolFault(-32603, "Response exceeds output budget", 500)
            await JSONResponse(response, headers={"Cache-Control": "no-store"})(scope, receive, send)
        except ProtocolFault as exc:
            await self._error(exc, request_id, scope, receive, send)
        except TimeoutError:
            await self._error(ProtocolFault(-32603, "Request deadline exceeded; retry writes identically", 504),
                              request_id, scope, receive, send)

    async def _execute_until_disconnect(self, method, params, receive):
        async def disconnected():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return

        execution = asyncio.create_task(self._execute(method, params))
        connection = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({execution, connection}, return_when=asyncio.FIRST_COMPLETED)
            if connection in done:
                # Disconnect may follow an already committed write. Its stable idempotency
                # key remains the recovery mechanism; cancellation cannot undo a commit.
                return None
            return await execution
        finally:
            for task in (execution, connection):
                if not task.done():
                    task.cancel()
            await asyncio.gather(execution, connection, return_exceptions=True)

    @staticmethod
    def _validate_modern(message, headers):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or _request_id(message) is None:
            raise ProtocolFault(-32600, "Expected one JSON-RPC request with a valid non-null id")
        if set(message) - {"jsonrpc", "id", "method", "params"}:
            raise ProtocolFault(-32600, "Unexpected JSON-RPC request members")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not method or not isinstance(params, dict):
            raise ProtocolFault(-32600, "Method and object params are required")
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            raise ProtocolFault(-32602, "Request metadata is required")
        version = meta.get(VERSION_KEY)
        if not isinstance(version, str) or not version:
            raise ProtocolFault(-32602, "Request metadata protocolVersion is required")
        if b"mcp-protocol-version" not in headers or b"mcp-method" not in headers:
            raise ProtocolFault(-32020, "Missing, malformed or inconsistent routing headers")
        if _decode_header(headers[b"mcp-protocol-version"]) != version:
            raise ProtocolFault(-32020, "Missing, malformed or inconsistent routing headers")
        DualProtocolRouter._validate_optional_routing(headers, message)
        if method in {"tools/call", "resources/read", "prompts/get"} and b"mcp-name" not in headers:
            raise ProtocolFault(-32020, "Missing, malformed or inconsistent routing headers")
        if version != MODERN_VERSION:
            raise ProtocolFault(-32022, "Unsupported protocol version for this request era", data={
                "supported": list(SUPPORTED_VERSIONS), "requested": version,
            })
        capabilities = meta.get(CAPABILITIES_KEY)
        if not isinstance(capabilities, dict):
            raise ProtocolFault(-32602, "Object clientCapabilities is required on every request")
        for known in {"roots", "sampling", "elicitation", "experimental", "extensions"}:
            if known in capabilities and not isinstance(capabilities[known], dict):
                raise ProtocolFault(-32602, "Malformed client capability declaration")
        for group in {"sampling", "elicitation", "experimental", "extensions"}:
            if group in capabilities and any(not isinstance(value, dict) for value in capabilities[group].values()):
                raise ProtocolFault(-32602, "Malformed client capability settings")
        if INFO_KEY in meta:
            info = meta[INFO_KEY]
            if not isinstance(info, dict) or any(not isinstance(info.get(k), str) or not info[k] for k in ("name", "version")):
                raise ProtocolFault(-32602, "Malformed self-reported clientInfo")
        accepted = headers.get(b"accept", b"").decode("ascii", errors="replace")
        media = set()
        for piece in accepted.split(","):
            fields = [field.strip().lower() for field in piece.split(";")]
            quality = 1.0
            for field in fields[1:]:
                if field.startswith("q="):
                    try:
                        quality = float(field[2:])
                    except ValueError as exc:
                        raise ProtocolFault(-32600, "Malformed Accept quality value", 406) from exc
                    if not 0 <= quality <= 1:
                        raise ProtocolFault(-32600, "Malformed Accept quality value", 406)
            if quality > 0:
                media.add(fields[0])
        if not {"application/json", "text/event-stream"}.issubset(media):
            raise ProtocolFault(-32600, "Accept must include application/json and text/event-stream", 406)
        content_type = headers.get(b"content-type", b"").decode("ascii", errors="replace").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ProtocolFault(-32600, "Content-Type must be application/json", 415)
        if method not in SUPPORTED_METHODS:
            raise ProtocolFault(-32601, "Method not found", 404)
        return method, params

    async def _execute(self, method: str, params: dict) -> dict[str, Any]:
        if method == "server/discover":
            if set(params) - {"_meta"}:
                raise ProtocolFault(-32602, "Unsupported discovery parameters")
            return {"supportedVersions": list(SUPPORTED_VERSIONS), "capabilities": {"tools": {}},
                    "instructions": self.instructions, "ttlMs": 0, "cacheScope": "private"}
        if method == "tools/list":
            if set(params) - {"_meta", "cursor"} or "cursor" in params:
                raise ProtocolFault(-32602, "Invalid cursor or unsupported list parameters")
            tools = await self.mcp.list_tools()
            descriptors = [tool.to_mcp_tool(execution=None).model_dump(mode="json", by_alias=True, exclude_none=True)
                           for tool in tools if tool.name in TOOL_MODELS]
            return {"tools": descriptors, "ttlMs": 0, "cacheScope": "private"}
        if set(params) - {"_meta", "name", "arguments"}:
            raise ProtocolFault(-32602, "Tasks and multi-round-trip parameters are not supported")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or name not in TOOL_MODELS:
            raise ProtocolFault(-32602, "Unknown tool")
        if not isinstance(arguments, dict) or set(arguments) - {"request"}:
            raise ProtocolFault(-32602, "Tool arguments accept only the advertised request field")
        # The optional sync request intentionally accepts {} and null: the shared tool
        # returns its actionable context checkpoint. manage_ledger still requires input.
        if name != "sync_context" or arguments.get("request") is not None:
            try:
                # Reject invalid data before the SDK's verbose argument-validation logger.
                TOOL_MODELS[name].model_validate(arguments.get("request"))
            except ValidationError as exc:
                raise ProtocolFault(-32602, "Invalid tool request; consult the advertised input schema") from exc
        try:
            result = await self.mcp.call_tool(name, arguments)
        except (NotFoundError, ToolValidationError, ValidationError) as exc:
            raise ProtocolFault(-32602, "Invalid tool name or arguments") from exc
        except ToolError as exc:
            # Hivemind's decorator supplies deliberately safe, actionable error codes.
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"content": [block.model_dump(mode="json", by_alias=True, exclude_none=True) for block in result.content],
                **({"structuredContent": result.structured_content} if result.structured_content is not None else {}),
                "isError": result.is_error, "_meta": result.meta or {}}

    @staticmethod
    async def _error(error, request_id, scope, receive, send):
        response = {"jsonrpc": "2.0", "error": {"code": error.code, "message": error.message}}
        if request_id is not None:
            response["id"] = request_id
        if error.data is not None:
            response["error"]["data"] = error.data
        await JSONResponse(response, status_code=error.status, headers={"Cache-Control": "no-store"})(scope, receive, send)
