"""Asynchronous click analytics.

The redirect must stay fast, so it never writes to Postgres itself. The pipeline is:

1. The redirect handler schedules ``ClickRecorder.record`` as a background task, which runs
   after the 302 has been sent.
2. ``record`` appends the raw event to a Redis Stream (one ``XADD``, sub-millisecond).
3. The worker process (``python -m app.worker``) reads the stream through a consumer group,
   parses user agents, and batch-inserts into ``clicks``.

Why a Redis Stream rather than FastAPI background tasks writing to Postgres directly? Events
survive API restarts and deploys, the worker can be scaled separately, and batching turns
thousands of single-row inserts into a few multi-row ones. Consumer groups give
at-least-once delivery; the unique ``event_id`` makes the insert idempotent, so a redelivered
event is not double-counted.

If Redis is unavailable, ``record`` falls back to inserting the single click directly so
analytics are not lost, at the cost of one database write per click during the outage.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from user_agents import parse as parse_user_agent

from app.core.config import Settings
from app.core.redis import RedisGateway, RedisUnavailableError
from app.models.click import DeviceType
from app.repositories.clicks import ClickRepository

logger = logging.getLogger(__name__)

MAX_USER_AGENT_LENGTH = 512
MAX_REFERRER_LENGTH = 2048


@dataclass(frozen=True, slots=True)
class ClickEvent:
    """What the redirect handler knows about a click, before any parsing."""

    link_id: int
    clicked_at: datetime
    referrer: str | None
    user_agent: str | None

    def to_fields(self) -> dict[str, str]:
        return {
            "link_id": str(self.link_id),
            "ts": self.clicked_at.isoformat(),
            "ref": (self.referrer or "")[:MAX_REFERRER_LENGTH],
            "ua": (self.user_agent or "")[:MAX_USER_AGENT_LENGTH],
        }

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> "ClickEvent":
        return cls(
            link_id=int(fields["link_id"]),
            clicked_at=datetime.fromisoformat(fields["ts"]),
            referrer=fields.get("ref") or None,
            user_agent=fields.get("ua") or None,
        )


def referrer_host(referrer: str | None) -> str | None:
    """Host part of a Referer header, lowercased; ``None`` for direct traffic or junk."""
    if not referrer:
        return None
    try:
        host = urlsplit(referrer).hostname
    except ValueError:
        return None
    return host[:255] if host else None


def classify_user_agent(user_agent: str | None) -> tuple[str, str, DeviceType]:
    """Return (browser family, OS family, device type) for a User-Agent string."""
    if not user_agent:
        return "Unknown", "Unknown", DeviceType.OTHER
    parsed = parse_user_agent(user_agent)
    if parsed.is_bot:
        device = DeviceType.BOT
    elif parsed.is_tablet:
        device = DeviceType.TABLET
    elif parsed.is_mobile:
        device = DeviceType.MOBILE
    elif parsed.is_pc:
        device = DeviceType.DESKTOP
    else:
        device = DeviceType.OTHER
    browser = str(parsed.browser.family or "Other")[:64]
    os_family = str(parsed.os.family or "Other")[:64]
    return browser, os_family, device


def build_click_row(event_id: str, event: ClickEvent) -> dict[str, Any]:
    browser, os_family, device = classify_user_agent(event.user_agent)
    return {
        "event_id": event_id,
        "link_id": event.link_id,
        "clicked_at": event.clicked_at,
        "referrer_host": referrer_host(event.referrer),
        "browser": browser,
        "os": os_family,
        "device_type": device,
    }


class ClickRecorder:
    """Enqueue click events; called after the redirect response has been sent."""

    def __init__(
        self,
        redis: RedisGateway,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._stream = settings.click_stream_key
        self._max_length = settings.click_stream_max_length

    async def record(self, event: ClickEvent) -> None:
        async def op(client: Redis) -> None:
            await client.xadd(
                self._stream,
                event.to_fields(),  # type: ignore[arg-type]  # redis stubs are invariant here
                maxlen=self._max_length,
                approximate=True,
            )

        try:
            await self._redis.run(op)
        except RedisUnavailableError:
            await self._write_directly(event)

    async def _write_directly(self, event: ClickEvent) -> None:
        row = build_click_row(f"direct-{uuid.uuid4().hex}", event)
        try:
            async with self._session_factory() as session:
                await ClickRepository(session).insert_many([row])
                await session.commit()
        except Exception:
            # Analytics must never break redirects; losing one click is the lesser evil.
            logger.exception("click_direct_write_failed", extra={"link_id": event.link_id})
            return
        logger.info("click_written_directly", extra={"link_id": event.link_id})


StreamEntries = Sequence[tuple[str, dict[str, str]]]


class ClickConsumer:
    """Moves click events from the Redis Stream into Postgres in batches."""

    def __init__(
        self,
        client: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        consumer_name: str,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._stream = settings.click_stream_key
        self._group = settings.click_consumer_group
        self._batch_size = settings.worker_batch_size
        self._claim_idle_ms = settings.worker_claim_idle_ms
        self._name = consumer_name

    async def ensure_group(self) -> None:
        """Create the consumer group (and the stream) if they do not exist yet."""
        try:
            await self._client.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def run_once(self, block_ms: int | None) -> int:
        """Process one batch. Returns the number of clicks inserted.

        First re-claims messages that another (probably crashed) consumer took but never
        acknowledged, then reads new ones. ``block_ms=None`` returns immediately if idle.
        """
        claimed = await self._client.xautoclaim(
            self._stream,
            self._group,
            self._name,
            min_idle_time=self._claim_idle_ms,
            start_id="0-0",
            count=self._batch_size,
        )
        entries: list[tuple[str, dict[str, str]]] = list(claimed[1])
        if len(entries) < self._batch_size:
            response = cast(
                list[tuple[str, StreamEntries]] | None,
                await self._client.xreadgroup(
                    self._group,
                    self._name,
                    {self._stream: ">"},
                    count=self._batch_size - len(entries),
                    block=block_ms,
                ),
            )
            for _stream, messages in response or []:
                entries.extend(messages)
        return await self.process(entries)

    async def process(self, entries: StreamEntries) -> int:
        """Insert a batch, then acknowledge and delete it from the stream.

        Acknowledging only after the commit is what makes delivery at-least-once: a crash
        between the two means the batch is delivered again, and ``event_id`` dedupes it.
        """
        if not entries:
            return 0
        rows: list[dict[str, Any]] = []
        for entry_id, fields in entries:
            try:
                rows.append(build_click_row(entry_id, ClickEvent.from_fields(fields)))
            except (KeyError, ValueError):
                # A malformed message can never succeed; dropping it beats blocking the queue.
                logger.warning("click_event_malformed", extra={"entry_id": entry_id})
        async with self._session_factory() as session:
            inserted = await ClickRepository(session).insert_many(rows)
            await session.commit()
        ids = [entry_id for entry_id, _ in entries]
        async with self._client.pipeline(transaction=False) as pipe:
            pipe.xack(self._stream, self._group, *ids)
            pipe.xdel(self._stream, *ids)
            await pipe.execute()
        logger.info(
            "clicks_ingested",
            extra={"received": len(entries), "inserted": inserted, "consumer": self._name},
        )
        return inserted
