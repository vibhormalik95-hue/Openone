"""Exercise unauthenticated DCR through the real SDK and encrypted Redis adapter."""
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from asgi_lifespan import LifespanManager
from cryptography.fernet import Fernet
from redis.asyncio import Redis
from test_oauth_integration import MemoryRedis, ReadyPool

import hivemind.app as application
from hivemind.config import Settings

CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"


class AdmissionRedis(MemoryRedis):
    def __init__(self):
        super().__init__()
        self.persisted = []
        self.admit = 1
        self.admissions = 0

    async def set(self, key=None, value=None, **kwargs):
        key = key or kwargs.pop("name")
        self.persisted.append((key, len(value), kwargs))
        return await super().set(key, value, **kwargs)

    async def setex(self, name, time, value):
        return await self.set(name, value, ex=time)

    async def get(self, key=None, **kwargs):
        return await super().get(key or kwargs["name"])

    async def eval(self, script, numkeys, *args):
        assert numkeys == 2
        assert all(key.startswith("hivemind:oauth:") for key in args[:2])
        assert "ZREMRANGEBYSCORE" in script
        self.admissions += 1
        return self.admit


@pytest.fixture
async def registration_client(monkeypatch):
    origin = "https://memory.example"
    config = {
        "AUTH_MODE": "oauth", "HVM_PUBLIC_ORIGIN": origin, "PUBLIC_ORIGIN": origin,
        "AUTH0_ISSUER": "https://identity.example/",
        "AUTH0_CLIENT_ID": "configuration-test-web-client",
        "AUTH0_LINK_CLIENT_ID": "configuration-test-link-client",
        "AUTH0_CLIENT_SECRET": secrets.token_urlsafe(48),
        "OAUTH_JWT_SIGNING_KEY": secrets.token_urlsafe(48),
        "OAUTH_STORAGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "OAUTH_REDIS_PASSWORD": secrets.token_urlsafe(48),
        "OAUTH_REDIRECT_URIS_JSON": '["' + CALLBACK + '"]',
    }
    for name, value in config.items():
        monkeypatch.setenv(name, value)
    redis = AdmissionRedis()
    monkeypatch.setattr(Redis, "from_url", lambda *args, **kwargs: redis)
    monkeypatch.setattr(application, "make_pool", lambda *args, **kwargs: ReadyPool())
    app = application.create_app(
        Settings(auth_mode="oauth", billing_database_url="test",
                 allowed_hosts=("memory.example",), public_base_url=origin),
        pool=ReadyPool(),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=origin,
        ) as client:
            yield client, redis


def registration(name="Hivemind registration test"):
    return {"redirect_uris": [CALLBACK], "client_name": name,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"]}


async def test_oversized_unauthenticated_registration_cannot_fill_redis(registration_client):
    client, redis = registration_client
    before = len(redis.persisted)
    response = await client.post("/register", json=registration("A" * 120000))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"
    assert len(redis.persisted) == before
    assert redis.admissions == 0


async def test_normal_registration_is_encrypted_and_expires(registration_client):
    client, redis = registration_client
    response = await client.post("/register", json=registration())
    assert response.status_code == 201
    assert redis.admissions == 1
    client_writes = [item for item in redis.persisted if "mcp-oauth-proxy-clients" in item[0]]
    assert len(client_writes) == 1
    assert client_writes[0][1] < 8192
    assert 0 < client_writes[0][2]["ex"] <= 30 * 24 * 60 * 60
    stored = redis.values[client_writes[0][0]]
    assert "Hivemind registration test" not in stored


@pytest.mark.parametrize("denial", [0, -1])
async def test_admission_refusal_does_not_persist_another_client(registration_client, denial):
    client, redis = registration_client
    redis.admit = denial
    before = len(redis.persisted)
    response = await client.post("/register", json=registration())
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"
    assert len(redis.persisted) == before


async def test_redirect_allowlist_remains_enforced(registration_client):
    client, redis = registration_client
    payload = registration()
    payload["redirect_uris"] = ["https://evil.example/callback"]
    response = await client.post("/register", json=payload)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"
    assert redis.admissions == 0


async def start_authorization(client, state="visible-client-correlation"):
    registered = await client.post("/register", json=registration())
    assert registered.status_code == 201
    return await client.get("/authorize", params={
        "client_id": registered.json()["client_id"], "redirect_uri": CALLBACK,
        "response_type": "code", "scope": "memory:access",
        "resource": "https://memory.example/mcp/v1",
        "code_challenge": "a" * 43, "code_challenge_method": "S256", "state": state,
    })


async def test_oversized_authorization_cannot_fill_challenge_store(registration_client):
    client, redis = registration_client
    response = await start_authorization(client, state="s" * 20000)
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.headers["location"]).query).get("error") == ["invalid_request"]
    assert not any("mcp-oauth-transactions" in item[0] for item in redis.persisted)


async def test_normal_authorization_reaches_consent_with_expiring_bounded_state(registration_client):
    client, redis = registration_client
    response = await start_authorization(client)
    assert response.status_code == 302
    assert urlsplit(response.headers["location"]).path == "/consent"
    challenges = [item for item in redis.persisted if "mcp-oauth-transactions" in item[0]]
    assert len(challenges) == 1
    assert challenges[0][1] < 16384
    assert 0 < challenges[0][2]["ex"] <= 15 * 60
    assert redis.admissions == 2
