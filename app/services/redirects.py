"""The redirect hot path: short code -> destination, Redis first, Postgres second."""

import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.repositories.links import LinkRepository
from app.services.cache import CachedAbsence, LinkCache, LinkTarget
from app.services.links import Clock, LinkGoneError, effective_status, utcnow

# Anything that cannot be a generated code or alias is rejected before touching storage.
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class RedirectService:
    def __init__(self, session: AsyncSession, cache: LinkCache, clock: Clock = utcnow) -> None:
        self._repo = LinkRepository(session)
        self._cache = cache
        self._clock = clock

    async def resolve(self, code: str) -> LinkTarget:
        """Return where ``code`` points, or raise 404 (unknown) / 410 (expired, disabled).

        On a cache hit no database connection is used at all: the session only checks out
        a connection from the pool when the first query runs.
        """
        if not CODE_PATTERN.fullmatch(code):
            raise NotFoundError
        cached = await self._cache.get(code)
        if cached is CachedAbsence.ABSENT:
            raise NotFoundError
        if cached is None:
            link = await self._repo.get_by_code(code)
            if link is None:
                await self._cache.set_absent(code)
                raise NotFoundError
            cached = LinkTarget(link.id, link.long_url, link.status, link.expires_at)
            await self._cache.set(code, cached)
        if effective_status(cached.status, cached.expires_at, self._clock()) != "active":
            raise LinkGoneError
        return cached
