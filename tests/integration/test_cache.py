import json
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import delete

from app.core.redis import RedisGateway
from app.main import create_app
from app.models.link import Link
from app.services.cache import LinkCache, cache_key
from tests.conftest import make_settings
from tests.integration.helpers import issue_key

LINKS = "/api/v1/links"


async def test_miss_populates_cache_then_hit_skips_the_database(
    app: FastAPI, client: AsyncClient
) -> None:
    redis = app.state.redis.client
    await client.post(LINKS, json={"url": "https://example.com/cached", "alias": "cached"})
    assert await redis.get(cache_key("cached")) is None  # creating does not warm the cache

    assert (await client.get("/cached")).status_code == 302  # miss -> Postgres -> cache
    entry = json.loads(await redis.get(cache_key("cached")))
    assert entry["url"] == "https://example.com/cached"
    assert 0 < await redis.ttl(cache_key("cached")) <= app.state.settings.cache_ttl_seconds

    # Remove the row behind the cache's back: a hit must not need the database.
    async with app.state.session_factory() as session:
        await session.execute(delete(Link).where(Link.code == "cached"))
        await session.commit()
    response = await client.get("/cached")
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/cached"


async def test_unknown_code_is_negatively_cached(app: FastAPI, client: AsyncClient) -> None:
    redis = app.state.redis.client
    assert (await client.get("/ghost01")).status_code == 404
    assert await redis.get(cache_key("ghost01")) == "absent"
    assert await redis.ttl(cache_key("ghost01")) <= app.state.settings.negative_cache_ttl_seconds
    assert (await client.get("/ghost01")).status_code == 404


async def test_creating_an_alias_clears_its_negative_cache_entry(client: AsyncClient) -> None:
    assert (await client.get("/later")).status_code == 404
    await client.post(LINKS, json={"url": "https://example.com", "alias": "later"})
    assert (await client.get("/later")).status_code == 302


async def test_disable_invalidates_the_cached_entry(client: AsyncClient) -> None:
    await client.post(LINKS, json={"url": "https://example.com", "alias": "soon-off"})
    assert (await client.get("/soon-off")).status_code == 302  # now cached
    await client.delete(f"{LINKS}/soon-off")
    assert (await client.get("/soon-off")).status_code == 410


async def test_patch_invalidates_the_cached_entry(app: FastAPI, client: AsyncClient) -> None:
    await client.post(LINKS, json={"url": "https://example.com", "alias": "patched"})
    await client.get("/patched")
    assert await app.state.redis.client.exists(cache_key("patched"))
    await client.patch(f"{LINKS}/patched", json={"expires_at": None})
    assert not await app.state.redis.client.exists(cache_key("patched"))


async def test_malformed_cache_entry_is_treated_as_a_miss(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post(LINKS, json={"url": "https://example.com/ok", "alias": "corrupt"})
    await app.state.redis.client.set(cache_key("corrupt"), "{not json")
    response = await client.get("/corrupt")
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/ok"


async def test_service_keeps_working_when_redis_is_down(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(make_settings(redis_url="redis://127.0.0.1:1/0"))
    with caplog.at_level(logging.WARNING):
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
        ):
            client.headers["X-API-Key"] = await issue_key(app)
            created = await client.post(LINKS, json={"url": "https://example.com/no-redis"})
            assert created.status_code == 201
            code = created.json()["code"]
            response = await client.get(f"/{code}")
            assert response.status_code == 302
            assert response.headers["location"] == "https://example.com/no-redis"
    events = [record.getMessage() for record in caplog.records]
    assert "redis_unavailable" in events
    assert "cache_invalidation_failed" in events


async def test_cache_methods_swallow_redis_outages() -> None:
    settings = make_settings(redis_url="redis://127.0.0.1:1/0")
    gateway = RedisGateway(Redis.from_url(settings.redis_url), cooldown_seconds=5)
    cache = LinkCache(gateway, ttl_seconds=10, negative_ttl_seconds=5)
    assert await cache.get("abc") is None
    await cache.set_absent("abc")
    await cache.invalidate("abc")
    await gateway.client.aclose()
