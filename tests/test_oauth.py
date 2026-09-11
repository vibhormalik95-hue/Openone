import time

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from hivemind.oauth import StrictJWTVerifier


@pytest.fixture(scope="module")
def signing_key():
    return RSAKey.generate_key(2048)


def verifier(key):
    return StrictJWTVerifier(public_key=key.as_pem(), algorithm="RS256",
        issuer="https://identity.example/", audience="https://memory.example/mcp/v1",
        required_scopes=["memory:access"])


def claims():
    now = int(time.time())
    return {"iss": "https://identity.example/", "sub": "auth0|paid-user",
            "aud": "https://memory.example/mcp/v1", "iat": now,
            "exp": now + 300, "scope": "openid memory:access"}


async def test_valid_signed_identity(signing_key):
    token = jwt.encode({"alg": "RS256"}, claims(), signing_key)
    verified = await verifier(signing_key).verify_token(token)
    assert verified is not None
    assert verified.claims["sub"] == "auth0|paid-user"


@pytest.mark.parametrize("field,value", [
    ("exp", None), ("iat", None), ("exp", 1),
    ("nbf", 9999999999), ("iat", 9999999999),
    ("iss", "https://other-identity.example/"),
    ("aud", "https://other-memory.example/mcp/v1"),
    ("scope", "openid"), ("sub", ""),
])
async def test_rejects_invalid_signed_claims(signing_key, field, value):
    payload = claims()
    if value is None:
        payload.pop(field, None)
    else:
        payload[field] = value
    token = jwt.encode({"alg": "RS256"}, payload, signing_key)
    assert await verifier(signing_key).verify_token(token) is None


async def test_rejects_signature_from_wrong_key(signing_key):
    other_key = RSAKey.generate_key(2048)
    token = jwt.encode({"alg": "RS256"}, claims(), other_key)
    assert await verifier(signing_key).verify_token(token) is None
