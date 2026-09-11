"""Environment settings. Reading this module does not connect to any service."""

import os
from dataclasses import dataclass, field

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536


def csv_env(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(part.strip() for part in os.getenv(name, default).split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default="", repr=False)
    billing_database_url: str = field(default="", repr=False)
    worker_database_url: str = field(default="", repr=False)
    openai_api_key: str = field(default="", repr=False)
    sentry_dsn: str = field(default="", repr=False)
    public_base_url: str = "http://localhost:8000"
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")
    allowed_origins: tuple[str, ...] = ()
    enable_legacy_sse: bool = False
    auth_mode: str = "api_key"
    max_request_bytes: int = 131072
    db_pool_max_size: int = 10

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("DATABASE_URL", ""),
            billing_database_url=os.getenv("BILLING_DATABASE_URL", ""),
            worker_database_url=os.getenv("WORKER_DATABASE_URL", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            sentry_dsn=os.getenv("SENTRY_DSN", ""),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
            allowed_hosts=csv_env("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"),
            allowed_origins=csv_env("ALLOWED_ORIGINS"),
            enable_legacy_sse=os.getenv("ENABLE_LEGACY_SSE", "false").lower() == "true",
            auth_mode=os.getenv("AUTH_MODE", "api_key"),
            db_pool_max_size=int(os.getenv("DB_POOL_MAX_SIZE", "10")),
        )
