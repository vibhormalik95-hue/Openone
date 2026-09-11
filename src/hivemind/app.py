"""ASGI composition: OAuth, pool lifecycle, bounded HTTP requests and operational routes."""

import asyncio
import logging
import os
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import FastAPI, Request
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from hivemind import __version__
from hivemind.auth import ApiKeyVerifier
from hivemind.config import Settings
from hivemind.engine import OpenAIEmbeddings
from hivemind.ledger import LedgerEngine
from hivemind.observability import configure_logging
from hivemind.protocol import DualProtocolRouter
from hivemind.server import SERVER_INSTRUCTIONS, create_mcp

logger = logging.getLogger("hivemind.http")
REQUEST_BODY_TIMEOUT = 15.0


class RequestGuard:
    """Pure ASGI guard preserves streaming and bounds request buffering."""

    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        is_mcp = path == "/mcp/v1" or path.startswith("/legacy/")
        headers = {key.lower(): value for key, value in scope["headers"]}
        if is_mcp:
            query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="ignore"))
            if "token" in query or "access_token" in query:
                tok = (query.get("token") or query.get("access_token"))[0]
                if b"authorization" not in headers:
                    headers[b"authorization"] = f"Bearer {tok}".encode("latin-1")
                    scope["headers"] = list(headers.items())
            origin = headers.get(b"origin")
            if origin and origin.decode("utf-8", errors="replace") not in self.settings.allowed_origins:
                return await JSONResponse({"error": "origin_denied"}, status_code=403)(
                    scope, receive, send
                )
        request_id = str(uuid4())
        started = time.monotonic()
        status = 500
        original_receive = receive
        if scope["method"] in ("POST", "PUT", "PATCH"):
            body = bytearray()
            try:
                async with asyncio.timeout(REQUEST_BODY_TIMEOUT):
                    while True:
                        message = await original_receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > self.settings.max_request_bytes:
                            return await JSONResponse({"error": "body_too_large"}, status_code=413)(
                                scope, original_receive, send
                            )
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await JSONResponse({"error": "request_body_timeout"}, status_code=408)(
                    scope, original_receive, send
                )
            pending_body = True

            async def bounded_receive():
                nonlocal pending_body
                if pending_body:
                    pending_body = False
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

            receive = bounded_receive

        async def response_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode()),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        try:
            await self.app(scope, receive, response_send)
        finally:
            # Unknown paths may themselves contain credentials. Never echo them.
            route = path if path in ("/mcp/v1", "/health/live", "/health/ready") else "other"
            logger.info(
                "http_request",
                extra={
                    "request_id": request_id,
                    "route": route,
                    "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )


async def configure_connection(connection):
    await register_vector_async(connection)
    await connection.commit()


def make_pool(dsn: str, *, maximum: int = 10, name: str = "hivemind-api"):
    return AsyncConnectionPool(
        dsn,
        min_size=1,
        max_size=maximum,
        open=False,
        configure=configure_connection,
        kwargs={"row_factory": dict_row, "connect_timeout": 10, "application_name": name},
    )


def create_app(settings: Settings | None = None, *, pool=None, engine=None, auth=None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.auth_mode == "oauth" and settings.enable_legacy_sse:
        raise ValueError("Legacy SSE is supported only in API-key mode; OAuth uses Streamable HTTP")
    owned_pool = pool is None
    pool = pool or make_pool(settings.database_url, maximum=settings.db_pool_max_size)
    billing_pool = (
        make_pool(settings.billing_database_url, maximum=3, name="hivemind-billing")
        if settings.billing_database_url else None
    )
    embeddings = OpenAIEmbeddings(settings.openai_api_key)
    engine = engine or LedgerEngine(pool, embeddings)
    if auth is None:
        auth = ApiKeyVerifier(pool)
        if settings.auth_mode == "oauth":
            from hivemind.oauth import build_auth

            if billing_pool is None:
                raise ValueError("BILLING_DATABASE_URL is required for AUTH_MODE=oauth")
            auth = build_auth(settings, billing_pool, api_key_verifier=auth)
        elif settings.auth_mode != "api_key":
            raise ValueError("AUTH_MODE must be api_key or oauth")
    mcp = create_mcp(engine, auth)
    mcp_app = mcp.http_app(path="/mcp/v1", stateless_http=True, json_response=False)
    legacy_app = None
    if settings.enable_legacy_sse:
        legacy = create_mcp(engine, auth)
        legacy_app = legacy.http_app(path="/sse", transport="sse")

    @asynccontextmanager
    async def lifespan(api):
        configure_logging(settings)
        if owned_pool and not settings.database_url:
            raise RuntimeError("DATABASE_URL is required")
        try:
            await pool.open(wait=True, timeout=30)
            if billing_pool is not None:
                await billing_pool.open(wait=True, timeout=30)
            api.state.billing_pool = billing_pool
            if settings.auth_mode == "oauth":
                await auth.ready()
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(mcp_app.lifespan(api))
                if legacy_app is not None:
                    await stack.enter_async_context(legacy_app.lifespan(api))
                yield
        finally:
            async with AsyncExitStack() as cleanup:
                cleanup.push_async_callback(pool.close)
                if billing_pool is not None:
                    cleanup.push_async_callback(billing_pool.close)
                cleanup.push_async_callback(embeddings.close)
                if settings.auth_mode == "oauth":
                    cleanup.push_async_callback(auth.close)

    api = FastAPI(title="Hivemind Scale", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    api.state.db_pool = pool
    api.state.mcp = mcp
    api.state.engine = engine
    api.state.settings = settings
    api.state.billing_pool = None

    @api.get("/health/live")
    async def live():
        return {"status": "ok", "version": __version__}

    @api.get("/health/ready")
    async def ready():
        try:
            async with pool.connection(timeout=2) as connection:
                cursor = await connection.execute("SELECT 1 AS healthy")
                await cursor.fetchone()
            if settings.auth_mode == "oauth":
                await auth.ready()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    from hivemind.billing import router

    api.include_router(router)
    web_dist = Path(os.getenv("WEB_DIST", "web/dist")).resolve()
    if (web_dist / "index.html").is_file():
        @api.get("/", include_in_schema=False)
        async def landing():
            return FileResponse(web_dist / "index.html")

        @api.get("/onboard/", include_in_schema=False)
        async def onboard_page():
            return FileResponse(web_dist / "onboard/index.html")

        @api.get("/account/", include_in_schema=False)
        async def account_page():
            return FileResponse(web_dist / "account/index.html")

        if (web_dist / "_astro").is_dir():
            api.mount("/_astro", StaticFiles(directory=web_dist / "_astro"))

    if settings.auth_mode == "oauth":
        from hivemind.oauth import browser_router, link_oauth_account

        api.state.oauth_auth = auth
        api.include_router(browser_router)

        @api.post("/account/link-oauth")
        async def link_identity(request: Request):
            if api.state.billing_pool is None:
                return JSONResponse({"error": "billing_unavailable"}, status_code=503)
            return await link_oauth_account(request, api.state.billing_pool)

    if legacy_app is not None:
        api.mount("/legacy", legacy_app)
    # Mount at root after operational/billing routes. /mcp/v1 is exact, with no trailing redirect.
    # OAuth well-known discovery routes stay at root, as required by the MCP authorization spec.
    api.mount("/", DualProtocolRouter(
        classic_app=mcp_app, mcp=mcp, auth=auth, instructions=SERVER_INSTRUCTIONS,
        resource_url=settings.public_base_url + "/mcp/v1",
        allowed_origins=settings.allowed_origins, max_request_bytes=settings.max_request_bytes,
    ))
    api.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    api.add_middleware(RequestGuard, settings=settings)
    return api
