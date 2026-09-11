"""Construct the deployed OAuth app with real SDKs and fake network dependencies."""
import secrets
from contextlib import asynccontextmanager

import httpx
from asgi_lifespan import LifespanManager
from cryptography.fernet import Fernet
from redis.asyncio import Redis

import hivemind.app as application
from hivemind.config import Settings


class MemoryRedis(Redis):
    def __init__(self):
        super().__init__()
        self.values = {}
        self.writes = 0
        self.closed = False
        self.fail = False

    async def set(self, key, value, **kwargs):
        if self.fail:
            raise ConnectionError("Storage unavailable")
        self.values[key] = value
        self.writes += 1
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)
        return 1

    async def aclose(self, **kwargs):
        self.closed = True
        await super().aclose(**kwargs)


class ReadyPool:
    def __init__(self):
        self.opened = False
        self.closed = False

    async def open(self, **kwargs):
        self.opened = True

    async def close(self):
        self.closed = True

    @asynccontextmanager
    async def connection(self, **kwargs):
        yield self

    async def execute(self, query):
        assert query == "SELECT 1 AS healthy"
        return self

    async def fetchone(self):
        return {"healthy": 1}


async def test_oauth_app_metadata_lifespan_mount_order_and_dependency_readiness(monkeypatch):
    origin = "https://memory.example"
    config = {
        "AUTH_MODE": "oauth",
        "HVM_PUBLIC_ORIGIN": origin,
        "PUBLIC_ORIGIN": origin,
        "AUTH0_ISSUER": "https://identity.example/",
        "AUTH0_CLIENT_ID": "configuration-test-web-client",
        "AUTH0_LINK_CLIENT_ID": "configuration-test-link-client",
        "AUTH0_CLIENT_SECRET": secrets.token_urlsafe(48),
        "OAUTH_JWT_SIGNING_KEY": secrets.token_urlsafe(48),
        "OAUTH_STORAGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "OAUTH_REDIS_PASSWORD": secrets.token_urlsafe(48),
        "OAUTH_REDIRECT_URIS_JSON": '["https://chatgpt.com/connector_platform_oauth_redirect"]',
    }
    for name, value in config.items():
        monkeypatch.setenv(name, value)
    redis = MemoryRedis()
    billing_pool, memory_pool = ReadyPool(), ReadyPool()
    monkeypatch.setattr(Redis, "from_url", lambda *args, **kwargs: redis)
    monkeypatch.setattr(application, "make_pool", lambda *args, **kwargs: billing_pool)
    settings = Settings(auth_mode="oauth", billing_database_url="configured-at-runtime",
                        allowed_hosts=("memory.example",), public_base_url=origin)
    app = application.create_app(settings, pool=memory_pool)
    async with LifespanManager(app):
        assert memory_pool.opened and billing_pool.opened
        assert redis.writes > 0
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            response = await client.get("/.well-known/oauth-protected-resource/mcp/v1")
            assert response.status_code == 200
            resource = response.json()
            assert resource["resource"] == origin + "/mcp/v1"
            assert resource["authorization_servers"] == [origin + "/"]
            response = await client.get("/.well-known/oauth-authorization-server")
            metadata = response.json()
            assert metadata["issuer"] == resource["authorization_servers"][0]
            assert metadata["token_endpoint"] == origin + "/token"
            assert "S256" in metadata["code_challenge_methods_supported"]
            response = await client.post("/mcp/v1", json={})
            assert response.status_code == 401
            assert origin + "/.well-known/oauth-protected-resource/mcp/v1" in response.headers["www-authenticate"]
            # This route must be matched before the root-mounted MCP application.
            response = await client.post("/account/link-oauth", json={})
            assert response.status_code == 401
            assert response.json()["error"] == "invalid_link_proof"
            # Mobile/browser linking must route to the authenticated control plane, not the MCP mount.
            for path, body in [
                ("start", {"key_id": "704439f0-3c5f-474f-a2db-7d91f6129182"}),
                ("poll", {"ticket": "t" * 43}),
            ]:
                response = await client.post("/account/oauth/" + path, json=body)
                assert response.status_code == 403
            assert app.state.oauth_auth is not None
            response = await client.get("/health/ready")
            assert response.status_code == 200
            redis.fail = True
            response = await client.get("/health/ready")
            assert response.status_code == 503
    assert billing_pool.closed and memory_pool.closed and redis.closed
