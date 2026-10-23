"""Data access for ``screening_events``."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.screening import ScreeningDecision, ScreeningEvent


class ScreeningRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        url: str,
        score: float,
        decision: ScreeningDecision,
        model_version: str,
        api_key_id: int,
        link_id: int | None,
    ) -> None:
        self._session.add(
            ScreeningEvent(
                url=url,
                score=score,
                decision=decision,
                model_version=model_version,
                api_key_id=api_key_id,
                link_id=link_id,
            )
        )
        await self._session.flush()
