"""Data access for ``clicks``: idempotent batch inserts and the stats aggregations."""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.click import Click


class ClickRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_many(self, rows: Sequence[dict[str, Any]]) -> int:
        """Insert rows, skipping event ids already stored. Returns rows actually inserted."""
        if not rows:
            return 0
        statement = (
            insert(Click)
            .values(list(rows))
            .on_conflict_do_nothing(index_elements=[Click.event_id])
            .returning(Click.id)
        )
        result = await self._session.execute(statement)
        return len(result.all())

    async def total(self, link_id: int) -> int:
        count = await self._session.scalar(
            select(func.count()).select_from(Click).where(Click.link_id == link_id)
        )
        return int(count or 0)

    async def daily(self, link_id: int, since: datetime) -> dict[date, int]:
        """Clicks per UTC day since ``since`` (days without clicks are absent)."""
        day = cast(func.timezone("UTC", Click.clicked_at), Date).label("day")
        rows = await self._session.execute(
            select(day, func.count())
            .where(Click.link_id == link_id, Click.clicked_at >= since)
            .group_by(day)
        )
        return {row[0]: int(row[1]) for row in rows}

    async def top_values(
        self, link_id: int, since: datetime, column: Any, limit: int
    ) -> list[tuple[Any, int]]:
        """Most frequent values of ``column`` since ``since``, most frequent first."""
        rows = await self._session.execute(
            select(column, func.count().label("clicks"))
            .where(Click.link_id == link_id, Click.clicked_at >= since)
            .group_by(column)
            .order_by(func.count().desc(), column)
            .limit(limit)
        )
        return [(row[0], int(row[1])) for row in rows]
