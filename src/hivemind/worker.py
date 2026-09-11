"""Durable PostgreSQL lease worker; run with python -m hivemind.worker."""

import asyncio
import logging
import signal

from pgvector import Vector
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from hivemind.config import EMBEDDING_MODEL, Settings
from hivemind.engine import EmbeddingUnavailable, OpenAIEmbeddings

logger = logging.getLogger("hivemind.worker")


async def configure_connection(connection):
    await register_vector_async(connection)
    await connection.commit()


async def run_one(pool: AsyncConnectionPool, embeddings: OpenAIEmbeddings) -> bool:
    # Claim commits before the external call. A crash is recovered after the 120-second lease.
    async with pool.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute("SELECT * FROM public.claim_embedding_jobs(1)")
            job = await cursor.fetchone()
    if job is None:
        return False
    try:
        vector = await embeddings.embed(job["content"])
        async with pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT public.finish_embedding_job(%s, %s, %s::vector, %s)",
                    (job["job_id"], job["lease_token"], Vector(vector), EMBEDDING_MODEL),
                )
    except EmbeddingUnavailable:
        # Store only a fixed error code. Provider errors may contain request or billing information.
        async with pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT public.fail_embedding_job(%s, %s, %s)",
                    (job["job_id"], job["lease_token"], "embedding_provider_unavailable"),
                )
    return True


async def main():
    from hivemind.observability import configure_logging

    settings = Settings.from_env()
    if not settings.worker_database_url or not settings.openai_api_key:
        raise RuntimeError("WORKER_DATABASE_URL and OPENAI_API_KEY are required")
    configure_logging(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    embeddings = OpenAIEmbeddings(settings.openai_api_key)
    pool = AsyncConnectionPool(
        settings.worker_database_url,
        open=False,
        min_size=1,
        max_size=2,
        configure=configure_connection,
        kwargs={"row_factory": dict_row, "connect_timeout": 10, "application_name": "hivemind-worker"},
    )
    try:
        await pool.open(wait=True, timeout=30)
        while not stop.is_set():
            try:
                found = await run_one(pool, embeddings)
            except Exception:
                # A DB outage leaves the lease recoverable; no job is deleted.
                logger.error("worker_iteration_failed")
                found = False
            if not found:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except TimeoutError:
                    pass
    finally:
        await embeddings.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
