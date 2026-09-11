"""Managed Auth0 login, FastMCP OAuth protocol, and explicit paid-account binding.

Optional AUTH_MODE=oauth. No Auth0 identity receives tenant access until linked.
Pinned against FastMCP 3.4.7; all business authorization remains in PostgreSQL.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, SupportsFloat
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException
from fastmcp.server.auth import MultiAuth, OAuthProxy, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from key_value.aio.wrappers.base import BaseWrapper
from mcp.server.auth.provider import AuthorizeError, RegistrationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

REGISTRATION_COLLECTION = "mcp-oauth-proxy-clients"
REGISTRATION_TTL_SECONDS = 30 * 24 * 60 * 60
REGISTRATION_MAX_CLIENTS = 4096
REGISTRATION_RATE_PER_MINUTE = 30
REGISTRATION_MAX_BYTES = 4096
CHALLENGE_COLLECTION = "mcp-oauth-transactions"
CHALLENGE_TTL_SECONDS = 15 * 60
CHALLENGE_MAX_COUNT = 2048
CHALLENGE_RATE_PER_MINUTE = 60
CHALLENGE_MAX_BYTES = 8192
# Redis TIME gives every replica the same admission clock. Reservations survive
# failed writes conservatively and expire; they never create an unlimited key set.
REGISTRATION_ADMISSION_LUA = """
local now = tonumber(redis.call('TIME')[1])
local ttl = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - ttl)
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  redis.call('ZADD', KEYS[1], now, ARGV[1])
  redis.call('EXPIRE', KEYS[1], ttl + 60)
  return 1
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return -1 end
local rate = tonumber(redis.call('GET', KEYS[2]) or '0')
if rate >= tonumber(ARGV[4]) then return 0 end
rate = redis.call('INCR', KEYS[2])
if rate == 1 then redis.call('EXPIRE', KEYS[2], 60) end
redis.call('ZADD', KEYS[1], now, ARGV[1])
redis.call('EXPIRE', KEYS[1], ttl + 60)
return 1
"""


class BoundedOAuthStorage(BaseWrapper):
    """Bound public clients and authorization challenges through the storage API.

    The collection name is verified against pinned FastMCP 3.4.7 by HTTP tests.
    Token/code collections retain SDK-controlled expiry and encryption.
    Client registrations expire 30 days after the last write. Native clients
    must register again after expiration; retaining an expired client forever
    would turn anonymous registration into unbounded durable storage.
    """

    def __init__(self, key_value, redis_client):
        self.key_value = key_value
        self.redis_client = redis_client

    async def put(
        self, key: str, value: Mapping[str, Any], *, collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        if collection in (REGISTRATION_COLLECTION, CHALLENGE_COLLECTION):
            if collection == REGISTRATION_COLLECTION:
                prefix = "hivemind:oauth:registration:"
                maximum_ttl, maximum_count = REGISTRATION_TTL_SECONDS, REGISTRATION_MAX_CLIENTS
                rate_limit, byte_limit = REGISTRATION_RATE_PER_MINUTE, REGISTRATION_MAX_BYTES
                error_class, error_code = RegistrationError, "invalid_client_metadata"
            else:
                prefix = "hivemind:oauth:challenge:"
                maximum_ttl, maximum_count = CHALLENGE_TTL_SECONDS, CHALLENGE_MAX_COUNT
                rate_limit, byte_limit = CHALLENGE_RATE_PER_MINUTE, CHALLENGE_MAX_BYTES
                error_class, error_code = AuthorizeError, "invalid_request"
            if len(json.dumps(dict(value), ensure_ascii=False).encode()) > byte_limit:
                raise error_class(error_code, "OAuth metadata exceeds the service limit")
            try:
                async with asyncio.timeout(3):
                    admission = await self.redis_client.eval(
                        REGISTRATION_ADMISSION_LUA, 2,
                        prefix + "entries", prefix + "rate",
                        hashlib.sha256(key.encode()).hexdigest(), maximum_ttl,
                        maximum_count, rate_limit,
                    )
            except Exception as exc:
                raise error_class(
                    error_code, "OAuth admission temporarily unavailable",
                ) from exc
            if admission != 1:
                raise error_class(
                    error_code, "OAuth admission limit reached; retry later",
                )
            ttl = min(float(ttl), maximum_ttl) if ttl is not None else maximum_ttl
        await self.key_value.put(key, value, collection=collection, ttl=ttl)

    async def put_many(
        self, keys: Sequence[str], values: Sequence[Mapping[str, Any]], *,
        collection: str | None = None, ttl: SupportsFloat | None = None,
    ) -> None:
        if len(keys) != len(values):
            raise ValueError("Keys and values must have equal length")
        for key, value in zip(keys, values, strict=True):
            await self.put(key, value, collection=collection, ttl=ttl)


class BoundedOAuthProxy(OAuthProxy):
    """Keep the SDK's OAuth protocol and redirect validation; bound metadata first."""

    async def register_client(self, client_info):
        name = client_info.client_name or ""
        redirects = client_info.redirect_uris or []
        if (len(name.encode()) > 200 or len(redirects) > 8
                or any(len(str(uri).encode()) > 2048 for uri in redirects)
                or len(client_info.model_dump_json().encode()) > REGISTRATION_MAX_BYTES):
            raise RegistrationError("invalid_client_metadata", "Client metadata exceeds the service limit")
        await super().register_client(client_info)

    async def authorize(self, client, params):
        if len(params.model_dump_json().encode()) > 4096:
            raise AuthorizeError("invalid_request", "Authorization metadata exceeds the service limit")
        return await super().authorize(client, params)


def secret(name: str) -> str:
    file = os.getenv(name + "_FILE")
    return Path(file).read_text().strip() if file else os.environ[name]


def public_origin() -> str:
    value = os.environ["HVM_PUBLIC_ORIGIN"].rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
            or parsed.fragment or parsed.username):
        raise ValueError("HVM_PUBLIC_ORIGIN must be an HTTPS origin without a path")
    return value


def issuer_url() -> str:
    value = os.environ["AUTH0_ISSUER"]
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path != "/"
            or parsed.query or parsed.fragment or parsed.username):
        raise ValueError("AUTH0_ISSUER must be the exact HTTPS issuer, including trailing slash")
    return value


class StrictJWTVerifier(JWTVerifier):
    """Add required temporal/subject checks to the SDK's RS256/JWKS validation."""

    async def verify_token(self, token: str):
        verified = await super().verify_token(token)
        if verified is None:
            return None
        claims = verified.claims
        now = time.time()
        exp, issued = claims.get("exp"), claims.get("iat")
        nbf = claims.get("nbf", issued)
        if any(type(value) not in (int, float) for value in (exp, issued, nbf)):
            return None
        if not (issued <= now + 30 and nbf <= now + 30 and exp > now and exp > issued):
            return None
        subject = claims.get("sub")
        if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
            return None
        return verified


def identity_verifier() -> StrictJWTVerifier:
    issuer = issuer_url()
    return StrictJWTVerifier(
        jwks_uri=issuer + ".well-known/jwks.json",
        issuer=issuer,
        audience=public_origin() + "/mcp/v1",
        algorithm="RS256",
        required_scopes=["memory:access"],
    )


class BoundIdentityVerifier(TokenVerifier):
    """Never infer tenant ownership from identity emails, names, or domains."""

    def __init__(self, pool, verifier=None):
        super().__init__(required_scopes=["memory:access"])
        self.pool = pool
        self.verifier = verifier or identity_verifier()

    async def verify_token(self, token: str):
        verified = await self.verifier.verify_token(token)
        if verified is None:
            return None
        pool = self.pool() if callable(self.pool) else self.pool
        if pool is None:
            return None
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT key_hash FROM public.lookup_oauth_identity(%s,%s)",
                (verified.claims["iss"], verified.claims["sub"]),
            )).fetchone()
        if not row:
            return None
        key_hash = row["key_hash"] if isinstance(row, dict) else row[0]
        return verified.model_copy(update={"claims": {
            **verified.claims, "key_hash": key_hash,
        }})


class ManagedOAuthAuth(MultiAuth):
    """OAuth provider with bounded dependency readiness and owned Redis lifecycle."""

    def __init__(self, *, redis_client, storage=None, **kwargs):
        super().__init__(**kwargs)
        self.redis_client = redis_client
        self.storage = storage

    async def ready(self):
        # Exercise authenticated writes too: PING cannot detect a full noeviction store.
        key = "hivemind:readiness:" + uuid4().hex
        async with asyncio.timeout(3):
            if not await self.redis_client.set(key, "ready", ex=10, nx=True):
                raise RuntimeError("OAuth storage write unavailable")
            if await self.redis_client.get(key) != "ready":
                raise RuntimeError("OAuth storage read unavailable")
            await self.redis_client.delete(key)

    async def close(self):
        async with asyncio.timeout(3):
            await self.redis_client.aclose()


def build_auth(settings, pool, api_key_verifier=None):
    """Factory called by the existing MCP app; adds no extra tools or chat UI."""
    if getattr(settings, "auth_mode", os.getenv("AUTH_MODE", "api_key")) != "oauth":
        if api_key_verifier is None:
            raise ValueError("api_key_verifier is required in API-key mode")
        return api_key_verifier
    from cryptography.fernet import Fernet
    from key_value.aio.stores.redis import RedisStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
    from redis.asyncio import Redis

    issuer = issuer_url()
    origin = public_origin()
    redirects = json.loads(os.environ["OAUTH_REDIRECT_URIS_JSON"])
    if not isinstance(redirects, list) or not redirects or any(
        not isinstance(uri, str) or "*" in uri or not uri.startswith("https://")
        for uri in redirects
    ):
        raise ValueError("Set exact HTTPS hosted-client callback URIs; no wildcard redirects")
    redis_client = Redis.from_url(
        os.getenv("OAUTH_REDIS_URL", "redis://oauth-redis:6379/0"),
        password=secret("OAUTH_REDIS_PASSWORD"),
        decode_responses=True,
        socket_timeout=3,
        socket_connect_timeout=3,
        max_connections=20,
    )
    storage = BoundedOAuthStorage(
        key_value=FernetEncryptionWrapper(
            key_value=RedisStore(client=redis_client),
            fernet=Fernet(secret("OAUTH_STORAGE_ENCRYPTION_KEY").encode()),
        ),
        redis_client=redis_client,
    )
    proxy = BoundedOAuthProxy(
        upstream_authorization_endpoint=issuer + "authorize",
        upstream_token_endpoint=issuer + "oauth/token",
        upstream_revocation_endpoint=issuer + "oauth/revoke",
        upstream_client_id=os.environ["AUTH0_CLIENT_ID"],
        upstream_client_secret=secret("AUTH0_CLIENT_SECRET"),
        token_verifier=BoundIdentityVerifier(pool),
        base_url=origin,
        resource_base_url=origin,
        issuer_url=origin,
        redirect_path="/auth/callback",
        allowed_client_redirect_uris=redirects,
        valid_scopes=["memory:access", "openid", "offline_access"],
        extra_authorize_params={
            "audience": origin + "/mcp/v1",
            "scope": "openid offline_access memory:access",
        },
        extra_token_params={"audience": origin + "/mcp/v1"},
        forward_pkce=True,
        forward_resource=False,  # Auth0 audience is fixed above; proxy validates MCP resource.
        token_endpoint_auth_method="client_secret_post",  # noqa: S106 -- protocol name
        client_storage=storage,
        jwt_signing_key=secret("OAUTH_JWT_SIGNING_KEY").encode(),
        require_authorization_consent=True,
        fastmcp_access_token_expiry_seconds=900,
        fallback_refresh_token_expiry_seconds=2592000,
        enable_cimd=True,
    )
    return ManagedOAuthAuth(
        server=proxy, verifiers=[api_key_verifier] if api_key_verifier else [],
        redis_client=redis_client, storage=storage,
    )


async def link_oauth_account(request: Request, billing_pool) -> JSONResponse:
    """Both proofs required: fresh IdP login plus possession of an active paid key.

    No ambient cookies are used, so cross-site requests cannot borrow a browser
    session. No permissive CORS. Web account pages can call this with the same
    two proofs; the included CLI supplies them after Auth0-managed device login.
    """
    headers = {"Cache-Control": "no-store"}
    if os.getenv("AUTH_MODE", "api_key") != "oauth":
        return JSONResponse({"error": "oauth_disabled"}, status_code=404, headers=headers)
    raw = await request.body()
    if len(raw) > 4096:
        return JSONResponse({"error": "request_too_large"}, status_code=413, headers=headers)
    try:
        body = json.loads(raw)
        key = body["api_key"]
        if not isinstance(key, str) or not key.startswith("hvm_") or not 32 <= len(key) <= 200:
            raise ValueError("invalid key")
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise ValueError("missing proof")
        identity = await identity_verifier().verify_token(auth[7:])
        if (identity is None
                or identity.claims.get("azp") != os.environ["AUTH0_LINK_CLIENT_ID"]
                or identity.claims["iat"] < time.time() - 600):
            raise ValueError("invalid identity")
    except (KeyError, ValueError, TypeError):
        return JSONResponse({"error": "invalid_link_proof"}, status_code=401, headers=headers)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    async with billing_pool.connection() as conn:
        async with conn.transaction():
            key_row = await (await conn.execute(
                """SELECT k.id FROM public.api_keys k JOIN public.tenants t ON t.id=k.tenant_id
                WHERE k.key_hash=%s AND k.revoked_at IS NULL
                AND (k.expires_at IS NULL OR k.expires_at>now())
                AND t.billing_status IN ('active','trialing') FOR UPDATE OF k""",
                (key_hash,),
            )).fetchone()
            if not key_row:
                return JSONResponse({"error": "invalid_link_proof"}, status_code=401, headers=headers)
            key_id = key_row["id"] if isinstance(key_row, dict) else key_row[0]
            binding = await (await conn.execute(
                """INSERT INTO public.oauth_identity_bindings(issuer,subject,key_id)
                VALUES (%s,%s,%s) ON CONFLICT(issuer,subject) DO UPDATE
                SET key_id=oauth_identity_bindings.key_id
                WHERE oauth_identity_bindings.key_id=EXCLUDED.key_id
                  AND oauth_identity_bindings.revoked_at IS NULL
                RETURNING key_id""",
                (identity.claims["iss"], identity.claims["sub"], key_id),
            )).fetchone()
            if not binding:
                return JSONResponse({"error": "identity_already_bound"}, status_code=409, headers=headers)
    return JSONResponse({"linked": True, "mcp_url": public_origin() + "/mcp/v1"}, headers=headers)


browser_router = APIRouter(prefix="/account/oauth", tags=["account identity"])
LINK_COLLECTION = "hivemind-browser-identity-links"


class BrowserLinkStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: UUID


class BrowserLinkPoll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: str = Field(min_length=40, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def browser_auth(request: Request) -> ManagedOAuthAuth:
    from hivemind.billing import same_origin
    same_origin(request)
    auth = getattr(request.app.state, "oauth_auth", None)
    if not isinstance(auth, ManagedOAuthAuth) or auth.storage is None:
        raise HTTPException(503, "Native sign-in is not configured")
    return auth


async def auth0_post(path: str, data: dict):
    # Only internal constant endpoint paths and a deployment-controlled HTTPS issuer.
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        try:
            response = await client.post(issuer_url() + path, data=data)
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Identity provider temporarily unavailable") from exc
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Expected an identity response object")
        return response.status_code, payload
    except ValueError as exc:
        raise HTTPException(503, "Identity provider returned an invalid response") from exc


async def browser_owner(request: Request):
    from hivemind.billing import account, billing_pool
    async with billing_pool(request).connection() as conn:
        return await account(conn, request, require_paid=True)


def session_digest(request: Request) -> str:
    from hivemind.billing import ACCOUNT_COOKIE
    return hashlib.sha256(request.cookies.get(ACCOUNT_COOKIE, "").encode()).hexdigest()


@browser_router.post("/start")
async def start_browser_link(body: BrowserLinkStart, request: Request):
    from hivemind.billing import billing_pool, one, private_response
    auth = browser_auth(request)
    owner = await browser_owner(request)
    async with billing_pool(request).connection() as conn:
        key = await one(conn, """SELECT id FROM api_keys WHERE id=%s AND tenant_id=%s
            AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now())""",
            (body.key_id, owner["id"]))
    if key is None:
        raise HTTPException(403, "Choose an active key belonging to your account")
    session_hash = session_digest(request)
    # One new upstream request per browser session per 30 seconds; shared across replicas.
    if not await auth.redis_client.set("hvm:link-start:" + session_hash, "1", nx=True, ex=30):
        raise HTTPException(429, "Wait 30 seconds before starting another sign-in")
    status, device = await auth0_post("oauth/device/code", {
        "client_id": os.environ["AUTH0_LINK_CLIENT_ID"],
        "audience": public_origin() + "/mcp/v1", "scope": "openid memory:access",
    })
    if status != 200:
        raise HTTPException(503, "Could not start identity sign-in")
    try:
        ttl = min(900, max(1, int(device["expires_in"])))
        interval = min(30, max(5, int(device.get("interval", 5))))
        verify_url = device.get("verification_uri_complete", device["verification_uri"])
        if urlsplit(verify_url).scheme != "https" or urlsplit(verify_url).netloc != urlsplit(issuer_url()).netloc:
            raise ValueError("Unexpected sign-in origin")
        device_code, user_code = device["device_code"], device["user_code"]
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(503, "Identity provider returned an invalid sign-in challenge") from exc
    ticket = secrets.token_urlsafe(32)
    state = {"tenant_id": str(owner["id"]), "key_id": str(body.key_id),
             "session_hash": session_hash, "device_code": device_code,
             "expires_at": time.time() + ttl, "interval": interval, "status": "pending"}
    await auth.storage.put(hashlib.sha256(ticket.encode()).hexdigest(), state,
                           collection=LINK_COLLECTION, ttl=ttl)
    return private_response({"ticket": ticket, "verification_uri_complete": verify_url,
                             "user_code": user_code, "interval": interval, "expires_in": ttl})


@browser_router.post("/poll")
async def poll_browser_link(body: BrowserLinkPoll, request: Request):
    from hivemind.billing import account, billing_pool, one, private_response
    auth = browser_auth(request)
    owner = await browser_owner(request)
    ticket_hash = hashlib.sha256(body.ticket.encode()).hexdigest()
    state = await auth.storage.get(ticket_hash, collection=LINK_COLLECTION)
    if (not state or state["expires_at"] <= time.time()
            or state["session_hash"] != session_digest(request)
            or state["tenant_id"] != str(owner["id"])):
        raise HTTPException(401, "Sign-in expired or belongs to another browser")
    if state["status"] == "linked":
        return private_response({"status": "linked", "mcp_url": public_origin() + "/mcp/v1"})
    # Serialize polls and enforce provider pacing across replicas. TTL exceeds network deadline.
    lock_key = "hvm:link-poll:" + ticket_hash
    if not await auth.redis_client.set(lock_key, "1", nx=True, ex=max(15, state["interval"])):
        return private_response({"status": "pending", "interval": max(15, state["interval"])}, 202)
    remaining = max(1, int(state["expires_at"] - time.time()))
    proof = state.get("verified_identity")
    if proof is None:
        status, token = await auth0_post("oauth/token", {
            "client_id": os.environ["AUTH0_LINK_CLIENT_ID"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": state["device_code"],
        })
        if status != 200:
            if token.get("error") in ("authorization_pending", "slow_down"):
                if token["error"] == "slow_down":
                    state["interval"] = min(60, state["interval"] + 5)
                    await auth.storage.put(ticket_hash, state, collection=LINK_COLLECTION, ttl=remaining)
                return private_response({"status": "pending", "interval": max(15, state["interval"])}, 202)
            await auth.storage.delete(ticket_hash, collection=LINK_COLLECTION)
            raise HTTPException(403, "Identity sign-in was denied or expired; start again")
        identity = await identity_verifier().verify_token(token.get("access_token", ""))
        if (identity is None or identity.claims.get("azp") != os.environ["AUTH0_LINK_CLIENT_ID"]
                or identity.claims["iat"] < time.time() - 600):
            await auth.storage.delete(ticket_hash, collection=LINK_COLLECTION)
            raise HTTPException(401, "Invalid identity proof")
        proof = {name: identity.claims[name] for name in ("iss", "sub", "exp")}
        # Persist verified provenance encrypted before SQL: database retries must not
        # try to consume the already-used provider device code a second time.
        state["verified_identity"] = proof
        state.pop("device_code", None)
        await auth.storage.put(ticket_hash, state, collection=LINK_COLLECTION, ttl=remaining)
    if proof["exp"] <= time.time():
        raise HTTPException(401, "Identity proof expired; start again")
    async with billing_pool(request).connection() as conn, conn.transaction():
        # Recheck owner, billing and key at binding time, not just when device flow started.
        fresh_owner = await account(conn, request, require_paid=True)
        key = await one(conn, """SELECT id FROM api_keys WHERE id=%s AND tenant_id=%s
            AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now()) FOR UPDATE""",
            (UUID(state["key_id"]), fresh_owner["id"]))
        if key is None or str(fresh_owner["id"]) != state["tenant_id"]:
            raise HTTPException(403, "Key or account authorization changed; start again")
        binding = await one(conn, """INSERT INTO oauth_identity_bindings(issuer,subject,key_id)
            VALUES(%s,%s,%s) ON CONFLICT(issuer,subject) DO UPDATE
            SET key_id=oauth_identity_bindings.key_id
            WHERE oauth_identity_bindings.key_id=EXCLUDED.key_id
              AND oauth_identity_bindings.revoked_at IS NULL RETURNING key_id""",
            (proof["iss"], proof["sub"], UUID(state["key_id"])))
        if binding is None:
            raise HTTPException(409, "Identity is already linked to another key")
    # Cache only the non-secret outcome for safe retry after a lost HTTP response.
    state.pop("device_code", None)
    state.pop("verified_identity", None)
    state["status"] = "linked"
    state["expires_at"] = time.time() + 300
    await auth.storage.put(ticket_hash, state, collection=LINK_COLLECTION, ttl=300)
    return private_response({"status": "linked", "mcp_url": public_origin() + "/mcp/v1"})
