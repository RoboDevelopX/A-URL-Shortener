import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.core.redis import RedisGateway
from app.main import create_app
from app.models.click import Click
from app.services.analytics import ClickConsumer, ClickEvent, ClickRecorder
from app.worker import run
from tests.conftest import make_settings
from tests.integration.helpers import issue_key
from tests.unit.test_analytics import CHROME_WINDOWS, GOOGLEBOT, IPHONE

LINKS = "/api/v1/links"


def make_consumer(app: FastAPI, name: str = "test-consumer", **overrides: object) -> ClickConsumer:
    settings = app.state.settings.model_copy(update=overrides)
    return ClickConsumer(app.state.redis.client, app.state.session_factory, settings, name)


async def click_count(app: FastAPI) -> int:
    async with app.state.session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Click)) or 0)


async def test_redirects_are_queued_then_ingested_into_stats(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post(LINKS, json={"url": "https://example.com", "alias": "tracked"})
    visits = [
        {"user-agent": IPHONE, "referer": "https://t.co/abc"},
        {"user-agent": IPHONE, "referer": "https://t.co/def"},
        {"user-agent": CHROME_WINDOWS, "referer": "https://news.ycombinator.com/"},
        {"user-agent": GOOGLEBOT},
    ]
    for headers in visits:
        assert (await client.get("/tracked", headers=headers)).status_code == 302

    # Nothing is in Postgres until the worker runs: the redirect only enqueued events.
    stream = app.state.settings.click_stream_key
    assert await app.state.redis.client.xlen(stream) == 4
    assert await click_count(app) == 0

    consumer = make_consumer(app)
    await consumer.ensure_group()
    assert await consumer.run_once(block_ms=None) == 4
    assert await app.state.redis.client.xlen(stream) == 0  # processed entries are deleted

    stats = (await client.get(f"{LINKS}/tracked/stats", params={"days": 7})).json()
    assert stats["total_clicks"] == 4
    assert stats["window_days"] == 7
    assert len(stats["daily"]) == 7
    assert stats["daily"][-1] == {"date": datetime.now(UTC).date().isoformat(), "clicks": 4}
    assert sum(day["clicks"] for day in stats["daily"]) == 4
    assert stats["top_referrers"][0] == {"referrer": "t.co", "clicks": 2}
    assert {"referrer": "(direct)", "clicks": 1} in stats["top_referrers"]
    assert stats["devices"] == {"desktop": 1, "mobile": 2, "tablet": 0, "bot": 1, "other": 0}
    assert stats["browsers"][0] == {"browser": "Mobile Safari", "clicks": 2}


async def test_stats_for_unknown_link_is_404(client: AsyncClient) -> None:
    assert (await client.get(f"{LINKS}/nope/stats")).status_code == 404


async def test_stats_window_is_validated(client: AsyncClient) -> None:
    await client.post(LINKS, json={"url": "https://example.com", "alias": "window"})
    assert (await client.get(f"{LINKS}/window/stats", params={"days": 0})).status_code == 422
    assert (await client.get(f"{LINKS}/window/stats", params={"days": 366})).status_code == 422


async def test_redelivered_events_are_counted_once(app: FastAPI, client: AsyncClient) -> None:
    await client.post(LINKS, json={"url": "https://example.com", "alias": "dupes"})
    await client.get("/dupes")
    redis = app.state.redis.client
    stream = app.state.settings.click_stream_key
    entries = await redis.xrange(stream)
    consumer = make_consumer(app)
    await consumer.ensure_group()

    # Simulate a crash after the insert but before the ack: the same batch arrives again.
    assert await consumer.process(entries) == 1
    assert await consumer.process(entries) == 0
    assert await click_count(app) == 1


async def test_unacknowledged_events_are_reclaimed_from_a_dead_consumer(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post(LINKS, json={"url": "https://example.com", "alias": "orphan"})
    await client.get("/orphan")
    redis = app.state.redis.client
    settings = app.state.settings
    dead = make_consumer(app, "dead-worker")
    await dead.ensure_group()
    # The dead worker reads the message and never acknowledges it.
    await redis.xreadgroup(
        settings.click_consumer_group, "dead-worker", {settings.click_stream_key: ">"}
    )

    survivor = make_consumer(app, "survivor", worker_claim_idle_ms=0)
    assert await survivor.run_once(block_ms=None) == 1
    pending = await redis.xpending(settings.click_stream_key, settings.click_consumer_group)
    assert pending["pending"] == 0


async def test_malformed_events_are_dropped_not_retried_forever(app: FastAPI) -> None:
    redis = app.state.redis.client
    stream = app.state.settings.click_stream_key
    await redis.xadd(stream, {"garbage": "1"})
    consumer = make_consumer(app)
    await consumer.ensure_group()
    assert await consumer.run_once(block_ms=None) == 0
    assert await redis.xlen(stream) == 0


async def test_ensure_group_is_idempotent(app: FastAPI) -> None:
    consumer = make_consumer(app)
    await consumer.ensure_group()
    await consumer.ensure_group()


async def test_clicks_are_written_directly_when_redis_is_down() -> None:
    app = create_app(make_settings(redis_url="redis://127.0.0.1:1/0"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        client.headers["X-API-Key"] = await issue_key(app)
        code = (await client.post(LINKS, json={"url": "https://example.com/x"})).json()["code"]
        assert (await client.get(f"/{code}", headers={"user-agent": IPHONE})).status_code == 302
        stats = (await client.get(f"{LINKS}/{code}/stats")).json()
    assert stats["total_clicks"] == 1
    assert stats["devices"]["mobile"] == 1


async def test_direct_write_failure_never_raises(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    dead_redis = RedisGateway(Redis.from_url("redis://127.0.0.1:1/0"), cooldown_seconds=5)
    recorder = ClickRecorder(dead_redis, app.state.session_factory, app.state.settings)
    # Link 999999 does not exist, so the fallback insert violates the foreign key.
    await recorder.record(ClickEvent(999_999, datetime.now(UTC), None, None))
    assert "click_direct_write_failed" in [r.getMessage() for r in caplog.records]
    await dead_redis.client.aclose()


async def test_worker_loop_ingests_and_stops_cleanly(app: FastAPI, client: AsyncClient) -> None:
    await client.post(LINKS, json={"url": "https://example.com", "alias": "loop"})
    await client.get("/loop")
    stop = asyncio.Event()
    worker = asyncio.create_task(
        run(app.state.settings.model_copy(update={"worker_block_ms": 50}), stop)
    )
    for _ in range(100):
        if await click_count(app) == 1:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(worker, timeout=5)
    assert await click_count(app) == 1


async def test_worker_backs_off_while_redis_is_down(caplog: pytest.LogCaptureFixture) -> None:
    stop = asyncio.Event()
    settings = make_settings(redis_url="redis://127.0.0.1:1/0")
    worker = asyncio.create_task(run(settings, stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(worker, timeout=5)
    assert "worker_iteration_failed" in [r.getMessage() for r in caplog.records]
