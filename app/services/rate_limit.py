"""Sliding-window rate limiting in Redis.

Algorithm: a "sliding window log". Each limited subject (an API key, an IP) has a sorted set
of request timestamps. A request is allowed if fewer than ``limit`` timestamps fall in the
last ``window`` milliseconds. Compared with a fixed window ("60 per calendar minute"), this
cannot be gamed by sending 60 requests at 12:00:59 and 60 more at 12:01:00.

The check-and-record runs as one Lua script, so it is atomic: two API instances cannot both
read "59 of 60" and both let a request through. All subjects are checked in the same script
and a request is recorded against all of them only if every one allows it. Redis' own clock
(``TIME``) is used so instances with skewed clocks agree.

Trade-off: memory is O(limit) per subject, fine for creation limits of tens or hundreds per
minute. For limits in the thousands, a sliding-window *counter* (two fixed buckets, weighted)
uses O(1) memory at the cost of being approximate.
"""

import logging
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from redis.asyncio import Redis

from app.core.redis import RedisGateway, RedisUnavailableError

logger = logging.getLogger(__name__)

# KEYS: one sorted set per subject. ARGV[1] = window in ms, ARGV[2] = unique member,
# ARGV[3..] = limit for each key. Returns 0 if allowed, else milliseconds until allowed.
SLIDING_WINDOW_LUA = """
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local window = tonumber(ARGV[1])
local wait = 0
for i, key in ipairs(KEYS) do
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
  if redis.call('ZCARD', key) >= tonumber(ARGV[i + 2]) then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local key_wait = math.max(tonumber(oldest[2]) + window - now, 1)
    if key_wait > wait then wait = key_wait end
  end
end
if wait > 0 then
  return wait
end
for _, key in ipairs(KEYS) do
  redis.call('ZADD', key, now, ARGV[2])
  redis.call('PEXPIRE', key, window)
end
return 0
"""


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """``limit`` requests per window for one subject, e.g. ``("key:12", 60)``."""

    subject: str
    limit: int


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class SlidingWindowRateLimiter:
    def __init__(self, redis: RedisGateway, window_seconds: int, namespace: str) -> None:
        self._redis = redis
        self._window_ms = window_seconds * 1000
        self._namespace = namespace
        self._script = redis.client.register_script(SLIDING_WINDOW_LUA)

    async def hit(self, rules: Sequence[RateLimitRule]) -> RateLimitDecision:
        """Count one request against every rule, or reject it without counting.

        Rejected requests are not recorded, so a client that keeps retrying while limited
        is let back in once its earlier requests age out of the window.
        """
        keys = [f"ratelimit:{self._namespace}:{rule.subject}" for rule in rules]
        args: list[int | str] = [self._window_ms, uuid.uuid4().hex, *(r.limit for r in rules)]

        async def op(client: Redis) -> int:
            return int(await self._script(keys=keys, args=args, client=client))

        try:
            wait_ms = await self._redis.run(op)
        except RedisUnavailableError:
            # Fail open: better to briefly allow unthrottled creates than to reject every
            # create because the limiter's storage is down. Abuse is still bounded by auth.
            logger.warning("rate_limit_skipped_redis_unavailable")
            return RateLimitDecision(allowed=True)
        if wait_ms == 0:
            return RateLimitDecision(allowed=True)
        return RateLimitDecision(allowed=False, retry_after_seconds=math.ceil(wait_ms / 1000))
