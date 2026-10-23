"""Redis access guarded by a small circuit breaker.

Redis is an accelerator here (cache, click queue, rate limits), never the source of truth.
When it goes away we want callers to fall back immediately. Without a breaker, every request
would first wait for a connection timeout before falling back, turning a Redis outage into a
latency outage. After a failure the breaker stays "open" for a cooldown, during which calls
fail fast; the first call after the cooldown probes Redis again.
"""

import logging
import time
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings

logger = logging.getLogger(__name__)


class RedisUnavailableError(Exception):
    """Raised instead of a raw Redis error so callers handle one exception type."""


class RedisGateway:
    """Run Redis operations, failing fast while Redis is known to be down."""

    def __init__(
        self,
        client: Redis,
        *,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._open_until = 0.0

    @property
    def is_open(self) -> bool:
        """True while the breaker is skipping Redis."""
        return self._clock() < self._open_until

    async def run[T](self, operation: Callable[[Redis], Awaitable[T]]) -> T:
        """Execute ``operation`` against Redis or raise ``RedisUnavailableError``."""
        if self.is_open:
            raise RedisUnavailableError("circuit open")
        try:
            result = await operation(self.client)
        except (RedisError, OSError) as exc:
            if self._open_until == 0.0:
                logger.warning("redis_unavailable", extra={"error": repr(exc)})
            self._open_until = self._clock() + self._cooldown
            raise RedisUnavailableError(str(exc)) from exc
        if self._open_until != 0.0:
            logger.info("redis_recovered")
            self._open_until = 0.0
        return result

    async def ping(self) -> bool:
        """Probe Redis directly, bypassing the breaker (used by the readiness check)."""
        try:
            return bool(await self.client.ping())
        except (RedisError, OSError):
            return False


def create_redis(settings: Settings) -> RedisGateway:
    """Build the client with tight timeouts and wrap it in the breaker."""
    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
    )
    return RedisGateway(client, cooldown_seconds=settings.redis_circuit_cooldown_seconds)
