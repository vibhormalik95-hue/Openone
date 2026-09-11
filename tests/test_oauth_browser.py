import hashlib
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastmcp.server.auth import AccessToken, TokenVerifier
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from hivemind import billing, oauth


class RejectVerifier(TokenVerifier):
    async def verify_token(self, token):
        return None


class BrowserRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, **kwargs):
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True


class BrowserPool:
    @asynccontextmanager
    async def connection(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield self


async def test_browser_link_requires_owner_origin_session_and_verified_identity(monkeypatch):
    origin = "https://memory.example"
    issuer = "https://identity.example/"
    tenant_id, key_id = uuid4(), uuid4()
    for name, value in {"AUTH_MODE": "oauth", "PUBLIC_ORIGIN": origin,
                        "HVM_PUBLIC_ORIGIN": origin, "AUTH0_ISSUER": issuer,
                        "AUTH0_LINK_CLIENT_ID": "native-link-client"}.items():
        monkeypatch.setenv(name, value)
    raw_store = MemoryStore()
    storage = FernetEncryptionWrapper(key_value=raw_store, fernet=Fernet(Fernet.generate_key()))
    auth = oauth.ManagedOAuthAuth(redis_client=BrowserRedis(), storage=storage,
                                  verifiers=[RejectVerifier()])
    app = FastAPI()
    app.state.oauth_auth = auth
    app.state.billing_pool = BrowserPool()
    app.include_router(oauth.browser_router)
    bindings = []
    provider_calls = []

    async def owner(conn, request, require_paid=False):
        if request.cookies.get(billing.ACCOUNT_COOKIE) not in ("owner-session", "other-session"):
            raise HTTPException(401, "Owner cookie required")
        assert require_paid
        return {"id": tenant_id, "billing_status": "active"}

    async def one(conn, query, params=()):
        if "INSERT INTO oauth_identity_bindings" in query:
            bindings.append(params)
            return {"key_id": key_id}
        assert params[1] == tenant_id
        return {"id": key_id} if params[0] == key_id else None

    async def provider(path, data):
        provider_calls.append(path)
        if path == "oauth/device/code":
            return 200, {"expires_in": 600, "interval": 5,
                         "verification_uri": issuer + "activate",
                         "verification_uri_complete": issuer + "activate?user_code=SAFE",
                         "device_code": "private-device-code", "user_code": "SAFE"}
        assert data["device_code"] == "private-device-code"
        return 200, {"access_token": "private-provider-token"}

    class VerifiedIdentity:
        async def verify_token(self, token):
            assert token == "private-provider-token"
            return AccessToken(token=token, client_id="native-link-client", scopes=["memory:access"],
                claims={"iss": issuer, "sub": "auth0|authenticated-owner", "exp": time.time() + 300,
                        "iat": time.time(), "azp": "native-link-client"})

    monkeypatch.setattr(billing, "account", owner)
    monkeypatch.setattr(billing, "one", one)
    monkeypatch.setattr(oauth, "auth0_post", provider)
    monkeypatch.setattr(oauth, "identity_verifier", VerifiedIdentity)
    headers = {"Origin": origin, "Cookie": billing.ACCOUNT_COOKIE + "=owner-session"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
        response = await client.post("/account/oauth/start", json={"key_id": str(key_id)})
        assert response.status_code == 403  # Cross-site/no-Origin denied before cookie authentication.
        response = await client.post("/account/oauth/start", headers={"Origin": origin},
                                     json={"key_id": str(key_id)})
        assert response.status_code == 401
        response = await client.post("/account/oauth/start", headers=headers,
                                     json={"key_id": str(uuid4())})
        assert response.status_code == 403
        response = await client.post("/account/oauth/start", headers=headers, json={"key_id": str(key_id)})
        assert response.status_code == 200
        result = response.json()
        assert "private-device-code" not in response.text
        ticket = result["ticket"]
        ticket_hash = hashlib.sha256(ticket.encode()).hexdigest()
        encrypted = await raw_store.get(ticket_hash, collection=oauth.LINK_COLLECTION)
        assert "private-device-code" not in repr(encrypted)
        response = await client.post("/account/oauth/start", headers=headers, json={"key_id": str(key_id)})
        assert response.status_code == 429
        foreign_headers = {"Origin": origin, "Cookie": billing.ACCOUNT_COOKIE + "=other-session"}
        response = await client.post("/account/oauth/poll", headers=foreign_headers, json={"ticket": ticket})
        assert response.status_code == 401  # Same owner/tenant, different browser still cannot consume.
        response = await client.post("/account/oauth/poll", headers=headers, json={"ticket": ticket})
        assert response.status_code == 200
        assert response.json() == {"status": "linked", "mcp_url": origin + "/mcp/v1"}
        assert "private-provider-token" not in response.text
        response = await client.post("/account/oauth/poll", headers=headers, json={"ticket": ticket})
        assert response.status_code == 200  # A lost response can be retried without another token grant.
    assert bindings == [(issuer, "auth0|authenticated-owner", key_id)]
    assert provider_calls == ["oauth/device/code", "oauth/token"]
