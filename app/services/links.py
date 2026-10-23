"""Link lifecycle: create, read, change expiry, disable."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.models.link import Link, LinkStatus
from app.repositories.links import DuplicateCodeError, LinkRepository
from app.schemas.links import EffectiveStatus, LinkCreate, LinkResponse
from app.services.cache import LinkCache
from app.services.codes import ShortCodeGenerator, validate_alias
from app.services.url_validation import URLPolicy, validate_target_url

logger = logging.getLogger(__name__)

# A generated code can only clash with a custom alias that happens to look like a generated
# code; each retry uses a fresh id. Hitting this limit would mean something is badly wrong.
MAX_CODE_ATTEMPTS = 5

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


class AliasConflictError(AppError):
    status_code = 409
    code = "alias_conflict"
    default_message = "Alias is already taken."


class InvalidExpiryError(AppError):
    status_code = 422
    code = "invalid_expiry"
    default_message = "expires_at must be in the future."


class LinkGoneError(AppError):
    status_code = 410
    code = "link_gone"
    default_message = "This link has expired or been disabled."


def effective_status(
    status: LinkStatus, expires_at: datetime | None, now: datetime
) -> EffectiveStatus:
    """Combine the stored status with expiry. Expiry is evaluated at read time, so there is
    no background job that has to flip rows to "expired" on schedule."""
    if status is LinkStatus.DISABLED:
        return "disabled"
    if expires_at is not None and expires_at <= now:
        return "expired"
    return "active"


def to_response(link: Link, origin: str, now: datetime) -> LinkResponse:
    return LinkResponse(
        code=link.code,
        short_url=f"{origin}/{link.code}",
        long_url=link.long_url,
        is_custom=link.is_custom,
        status=effective_status(link.status, link.expires_at, now),
        expires_at=link.expires_at,
        created_at=link.created_at,
    )


class LinkService:
    """Business rules for links. Raises ``AppError`` subclasses; knows nothing about HTTP."""

    def __init__(
        self,
        session: AsyncSession,
        codes: ShortCodeGenerator,
        cache: LinkCache,
        settings: Settings,
        clock: Clock = utcnow,
    ) -> None:
        self._session = session
        self._repo = LinkRepository(session)
        self._codes = codes
        self._cache = cache
        self._clock = clock
        self._url_policy = URLPolicy(
            max_length=settings.max_url_length,
            own_hosts=frozenset({settings.public_base_url.host or ""}),
        )

    def _check_expiry(self, expires_at: datetime | None) -> datetime | None:
        if expires_at is not None and expires_at <= self._clock():
            raise InvalidExpiryError
        return expires_at

    async def create(self, data: LinkCreate) -> Link:
        """Validate and store a new link, returning it with its code."""
        long_url = validate_target_url(data.url, self._url_policy)
        expires_at = self._check_expiry(data.expires_at)

        if data.alias is not None:
            link = Link(
                code=validate_alias(data.alias),
                long_url=long_url,
                is_custom=True,
                expires_at=expires_at,
            )
            try:
                await self._repo.insert(link)
            except DuplicateCodeError:
                raise AliasConflictError(f"Alias '{data.alias}' is already taken.") from None
        else:
            link = await self._insert_generated(long_url, expires_at)

        await self._session.commit()
        # Clears a cached 404 for this code (someone may have probed an alias before it
        # existed), so the new link is reachable immediately.
        await self._cache.invalidate(link.code)
        logger.info("link_created", extra={"code": link.code, "is_custom": link.is_custom})
        return link

    async def _insert_generated(self, long_url: str, expires_at: datetime | None) -> Link:
        for _ in range(MAX_CODE_ATTEMPTS):
            link_id = await self._repo.next_id()
            link = Link(
                id=link_id,
                code=self._codes.encode(link_id),
                long_url=long_url,
                is_custom=False,
                expires_at=expires_at,
            )
            try:
                await self._repo.insert(link)
            except DuplicateCodeError:
                logger.warning("generated_code_taken_by_alias", extra={"code": link.code})
                continue
            return link
        raise AppError("Could not allocate a short code.")

    async def get(self, code: str) -> Link:
        link = await self._repo.get_by_code(code)
        if link is None:
            raise NotFoundError(f"Link '{code}' not found.")
        return link

    async def update_expiry(self, code: str, expires_at: datetime | None) -> Link:
        link = await self.get(code)
        if link.status is LinkStatus.DISABLED:
            raise LinkGoneError("Disabled links cannot be changed.")
        link.expires_at = self._check_expiry(expires_at)
        await self._session.commit()
        await self._cache.invalidate(code)
        await self._session.refresh(link)
        return link

    async def disable(self, code: str) -> None:
        """Soft-delete. Idempotent: disabling a disabled link is a no-op."""
        link = await self.get(code)
        if link.status is not LinkStatus.DISABLED:
            link.status = LinkStatus.DISABLED
            await self._session.commit()
            await self._cache.invalidate(code)
            logger.info("link_disabled", extra={"code": code})
