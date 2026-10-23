"""Click-ingestion worker: ``python -m app.worker``.

Runs as its own process (its own container in docker compose) so ingestion can be scaled
and restarted independently of the API. Several workers can run at once: the consumer group
gives each message to exactly one of them.
"""

import asyncio
import contextlib
import logging
import signal
import socket

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_session_factory
from app.core.logging import configure_logging
from app.services.analytics import ClickConsumer

logger = logging.getLogger("app.worker")

MAX_BACKOFF_SECONDS = 30.0


async def run(settings: Settings, stop: asyncio.Event) -> None:
    """Consume until ``stop`` is set, backing off exponentially while a dependency is down."""
    engine = create_engine(settings)
    # The read blocks for up to worker_block_ms, so the socket timeout must be longer than
    # that; the API's short timeouts would turn every idle poll into an error.
    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.worker_block_ms / 1000 + 5,
    )
    consumer = ClickConsumer(
        client, create_session_factory(engine), settings, consumer_name=socket.gethostname()
    )
    backoff = 0.5
    group_ready = False
    logger.info("worker_started")
    try:
        while not stop.is_set():
            try:
                if not group_ready:
                    await consumer.ensure_group()
                    group_ready = True
                await consumer.run_once(block_ms=settings.worker_block_ms)
                backoff = 0.5
            except (RedisError, SQLAlchemyError, OSError) as exc:
                # Re-create the group on the next attempt: if Redis restarted without its
                # data, the group is gone and every read would fail with NOGROUP.
                group_ready = False
                logger.warning(
                    "worker_iteration_failed", extra={"error": repr(exc), "retry_in_s": backoff}
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    finally:
        await client.aclose()
        await engine.dispose()
        logger.info("worker_stopped")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await run(settings, stop)


if __name__ == "__main__":
    asyncio.run(main())
