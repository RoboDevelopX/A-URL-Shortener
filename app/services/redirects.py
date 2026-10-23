"""The redirect hot path: short code -> destination."""

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.link import LinkStatus
from app.repositories.links import LinkRepository
from app.services.links import Clock, LinkGoneError, effective_status, utcnow

# Anything that cannot be a generated code or alias is rejected before touching storage.
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """The minimum needed to answer a redirect."""

    link_id: int
    long_url: str
    status: LinkStatus
    expires_at: datetime | None


class RedirectService:
    def __init__(self, session: AsyncSession, clock: Clock = utcnow) -> None:
        self._repo = LinkRepository(session)
        self._clock = clock

    async def resolve(self, code: str) -> LinkTarget:
        """Return where ``code`` points, or raise 404 (unknown) / 410 (expired, disabled)."""
        if not CODE_PATTERN.fullmatch(code):
            raise NotFoundError
        link = await self._repo.get_by_code(code)
        if link is None:
            raise NotFoundError
        target = LinkTarget(link.id, link.long_url, link.status, link.expires_at)
        if effective_status(target.status, target.expires_at, self._clock()) != "active":
            raise LinkGoneError
        return target
