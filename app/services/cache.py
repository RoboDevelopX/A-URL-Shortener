"""Cache-aside storage of redirect targets in Redis.

Read path: look in Redis; on a miss read Postgres and write the answer back. Write path:
commit to Postgres first, then delete the cache key, so the next read repopulates it.
Deleting (rather than overwriting) on writes means two concurrent writers cannot leave the
cache holding the older of their two values.

Every method treats Redis as optional: if it is unavailable, reads behave like a miss and
writes are skipped. The redirect still works, just from Postgres.
"""

import enum
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from redis.asyncio import Redis

from app.core.redis import RedisGateway, RedisUnavailableError
from app.models.link import LinkStatus

logger = logging.getLogger(__name__)

_ABSENT_VALUE = "absent"


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """The minimum needed to answer a redirect; this is what the cache stores."""

    link_id: int
    long_url: str
    status: LinkStatus
    expires_at: datetime | None


class CachedAbsence(enum.Enum):
    """Marker for a cached "this code does not exist"."""

    ABSENT = enum.auto()


def cache_key(code: str) -> str:
    return f"link:{code}"


def _serialise(target: LinkTarget) -> str:
    return json.dumps(
        {
            "id": target.link_id,
            "url": target.long_url,
            "status": target.status.value,
            "expires_at": target.expires_at.isoformat() if target.expires_at else None,
        }
    )


def _deserialise(raw: str) -> LinkTarget:
    data = json.loads(raw)
    expires_at = data["expires_at"]
    return LinkTarget(
        link_id=int(data["id"]),
        long_url=str(data["url"]),
        status=LinkStatus(data["status"]),
        expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
    )


class LinkCache:
    """Redis-backed cache of ``code -> LinkTarget``."""

    def __init__(self, redis: RedisGateway, ttl_seconds: int, negative_ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._negative_ttl = negative_ttl_seconds

    async def get(self, code: str) -> LinkTarget | CachedAbsence | None:
        """Cached target, ``CachedAbsence.ABSENT`` for a cached 404, or ``None`` on a miss."""

        async def op(client: Redis) -> str | None:
            # The client is built with decode_responses=True, so values are str.
            return cast(str | None, await client.get(cache_key(code)))

        try:
            raw = await self._redis.run(op)
        except RedisUnavailableError:
            return None
        if raw is None:
            return None
        if raw == _ABSENT_VALUE:
            return CachedAbsence.ABSENT
        try:
            return _deserialise(raw)
        except (ValueError, KeyError, TypeError):
            # A malformed entry (e.g. written by an older release) is just a miss.
            logger.warning("cache_entry_invalid", extra={"code": code})
            return None

    async def set(self, code: str, target: LinkTarget) -> None:
        """Store a target. Expiry is re-checked on every read, so the TTL is only about
        memory and staleness, not correctness of the 410 for expired links."""
        await self._write(code, _serialise(target), self._ttl)

    async def set_absent(self, code: str) -> None:
        await self._write(code, _ABSENT_VALUE, self._negative_ttl)

    async def invalidate(self, code: str) -> None:
        """Drop the entry after a write to Postgres."""

        async def op(client: Redis) -> None:
            await client.delete(cache_key(code))

        try:
            await self._redis.run(op)
        except RedisUnavailableError:
            # The entry (if any) expires after ``cache_ttl_seconds``: that is the bound on
            # staleness while Redis is unreachable.
            logger.error("cache_invalidation_failed", extra={"code": code})

    async def _write(self, code: str, value: str, ttl: int) -> None:
        async def op(client: Redis) -> None:
            await client.set(cache_key(code), value, ex=ttl)

        try:
            await self._redis.run(op)
        except RedisUnavailableError:
            return
