"""Rate limiting. The limiter's logic is a Lua script, so it is tested against real Redis."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.api.deps import get_screener
from app.core.redis import RedisGateway
from app.main import create_app
from app.services.rate_limit import RateLimitRule, SlidingWindowRateLimiter
from tests.conftest import make_settings
from tests.integration.helpers import issue_key
from tests.stubs import allow_all_screener

LINKS = "/api/v1/links"


@pytest.fixture
async def gateway(app: FastAPI) -> RedisGateway:  # depends on app: Redis is flushed
    gw: RedisGateway = app.state.redis
    return gw


async def test_allows_up_to_the_limit_then_rejects(gateway: RedisGateway) -> None:
    limiter = SlidingWindowRateLimiter(gateway, window_seconds=60, namespace="t")
    rule = [RateLimitRule("subject", 3)]
    results = [await limiter.hit(rule) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert 59 <= results[-1].retry_after_seconds <= 60


async def test_window_slides(gateway: RedisGateway) -> None:
    limiter = SlidingWindowRateLimiter(gateway, window_seconds=1, namespace="t")
    rule = [RateLimitRule("slide", 2)]
    assert (await limiter.hit(rule)).allowed
    assert (await limiter.hit(rule)).allowed
    rejected = await limiter.hit(rule)
    assert not rejected.allowed
    assert rejected.retry_after_seconds == 1
    await asyncio.sleep(1.05)
    assert (await limiter.hit(rule)).allowed


async def test_subjects_are_independent(gateway: RedisGateway) -> None:
    limiter = SlidingWindowRateLimiter(gateway, window_seconds=60, namespace="t")
    assert (await limiter.hit([RateLimitRule("a", 1)])).allowed
    assert not (await limiter.hit([RateLimitRule("a", 1)])).allowed
    assert (await limiter.hit([RateLimitRule("b", 1)])).allowed


async def test_rejection_by_one_rule_counts_against_none(gateway: RedisGateway) -> None:
    limiter = SlidingWindowRateLimiter(gateway, window_seconds=60, namespace="t")
    assert (await limiter.hit([RateLimitRule("tight", 1)])).allowed
    # "tight" is full, so this request is rejected and must not consume a "loose" slot.
    assert not (await limiter.hit([RateLimitRule("loose", 1), RateLimitRule("tight", 1)])).allowed
    assert (await limiter.hit([RateLimitRule("loose", 1)])).allowed


async def test_concurrent_hits_never_exceed_the_limit(gateway: RedisGateway) -> None:
    limiter = SlidingWindowRateLimiter(gateway, window_seconds=60, namespace="t")
    results = await asyncio.gather(*(limiter.hit([RateLimitRule("race", 10)]) for _ in range(50)))
    assert sum(r.allowed for r in results) == 10


async def test_fails_open_when_redis_is_down() -> None:
    gateway = RedisGateway(Redis.from_url("redis://127.0.0.1:1/0"), cooldown_seconds=5)
    limiter = SlidingWindowRateLimiter(gateway, window_seconds=60, namespace="t")
    assert (await limiter.hit([RateLimitRule("x", 1)])).allowed
    await gateway.client.aclose()


@pytest.fixture
async def limited_app() -> AsyncIterator[FastAPI]:
    app = create_app(make_settings(rate_limit_create_per_key=3, rate_limit_create_per_ip=5))
    app.dependency_overrides[get_screener] = allow_all_screener
    async with app.router.lifespan_context(app):
        await app.state.redis.client.flushdb()
        yield app


async def test_create_returns_429_with_retry_after_per_key(limited_app: FastAPI) -> None:
    transport = ASGITransport(app=limited_app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        first = {"X-API-Key": await issue_key(limited_app, "first")}
        second = {"X-API-Key": await issue_key(limited_app, "second")}
        statuses = [
            (await client.post(LINKS, json={"url": "https://e.com"}, headers=first)).status_code
            for _ in range(4)
        ]
        assert statuses == [201, 201, 201, 429]

        limited = await client.post(LINKS, json={"url": "https://e.com"}, headers=first)
        assert limited.json()["error"]["code"] == "rate_limited"
        assert 1 <= int(limited.headers["retry-after"]) <= 60

        # A different key from the same IP still has its own budget: until the IP limit
        # (5 per window, 3 already used) is reached.
        ok = [
            (await client.post(LINKS, json={"url": "https://e.com"}, headers=second)).status_code
            for _ in range(3)
        ]
        assert ok == [201, 201, 429]
