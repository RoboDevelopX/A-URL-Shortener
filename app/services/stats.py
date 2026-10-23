"""Per-link click statistics."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.click import Click, DeviceType
from app.models.link import Link
from app.repositories.clicks import ClickRepository
from app.schemas.stats import BrowserCount, DailyClicks, LinkStats, ReferrerCount
from app.services.links import Clock, utcnow

TOP_REFERRERS = 10
TOP_BROWSERS = 5
DIRECT = "(direct)"


class StatsService:
    def __init__(self, session: AsyncSession, clock: Clock = utcnow) -> None:
        self._clicks = ClickRepository(session)
        self._clock = clock

    async def for_link(self, link: Link, days: int) -> LinkStats:
        """Stats over the last ``days`` UTC days (including today), plus the all-time total.

        Clicks arrive through the async pipeline, so counts lag real time by roughly the
        worker's polling interval (about a second).
        """
        today = self._clock().astimezone(UTC).date()
        first_day = today - timedelta(days=days - 1)
        since = datetime.combine(first_day, datetime.min.time(), tzinfo=UTC)
        window = [first_day + timedelta(days=offset) for offset in range(days)]

        per_day = await self._clicks.daily(link.id, since)
        referrers = await self._clicks.top_values(
            link.id, since, Click.referrer_host, TOP_REFERRERS
        )
        devices = await self._clicks.top_values(link.id, since, Click.device_type, len(DeviceType))
        browsers = await self._clicks.top_values(link.id, since, Click.browser, TOP_BROWSERS)

        return LinkStats(
            code=link.code,
            window_days=days,
            total_clicks=await self._clicks.total(link.id),
            daily=[DailyClicks(date=day, clicks=per_day.get(day, 0)) for day in window],
            top_referrers=[ReferrerCount(referrer=r or DIRECT, clicks=n) for r, n in referrers],
            devices={device.value: 0 for device in DeviceType}
            | {DeviceType(d).value: n for d, n in devices},
            browsers=[BrowserCount(browser=b, clicks=n) for b, n in browsers],
        )
