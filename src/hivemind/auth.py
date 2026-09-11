"""Opaque personal tokens for header-capable beta clients.

This is deliberately not an OAuth authorization server. Hosted-client OAuth uses oauth.py.
"""

import hashlib
import logging
import re
from contextlib import asynccontextmanager

from fastmcp.server.auth import AccessToken, TokenVerifier
from psycopg import errors
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("hivemind.auth")
TOKEN_PATTERN = re.compile(r"^hvm_[A-Za-z0-9_-]{40,128}$")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthenticationFailed(Exception):
    """Credential not valid for the current transaction."""


@asynccontextmanager
async def authenticated_connection(pool: AsyncConnectionPool, key_hash: str):
    """Bind identity in the exact transaction performing a read/write; never SET globally."""
    async with pool.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                "SELECT * FROM public.authenticate_api_key(%s)", (key_hash,)
            )
            if await cursor.fetchone() is None:
                raise AuthenticationFailed("invalid_or_revoked_key")
            yield connection


class ApiKeyVerifier(TokenVerifier):
    def __init__(self, pool: AsyncConnectionPool):
        super().__init__(required_scopes=["memory:access"])
        self.pool = pool

    async def verify_token(self, token: str) -> AccessToken | None:
        if not TOKEN_PATTERN.fullmatch(token):
            return None
        key_hash = token_digest(token)
        try:
            async with self.pool.connection() as connection:
                async with connection.transaction():
                    cursor = await connection.execute(
                        "SELECT * FROM public.authenticate_api_key(%s)", (key_hash,)
                    )
                    row = await cursor.fetchone()
        except (errors.InvalidAuthorizationSpecification, errors.InsufficientPrivilege):
            return None
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(row["key_id"]),
            scopes=["memory:access"],
            claims={
                "key_id": str(row["key_id"]),
                "tenant_id": str(row["tenant_id"]),
                "key_hash": key_hash,
            },
        )
