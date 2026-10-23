"""Data access for ``links``. All SQL is built by SQLAlchemy, so values are always bound
parameters, never string-formatted into queries."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.link import LINKS_ID_SEQ, Link

CODE_UNIQUE_CONSTRAINT = "uq_links_code"


class DuplicateCodeError(Exception):
    """The code is already used by another link."""


def _violated_constraint(exc: IntegrityError) -> str | None:
    # asyncpg's UniqueViolationError (the DBAPI error's cause) carries the constraint name.
    cause = getattr(exc.orig, "__cause__", None)
    name = getattr(cause, "constraint_name", None)
    return name if isinstance(name, str) else None


class LinkRepository:
    """Queries over ``links``. Repositories flush; services own the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def next_id(self) -> int:
        """Reserve the next id from the sequence (never returned twice, even on rollback)."""
        value = await self._session.scalar(select(LINKS_ID_SEQ.next_value()))
        if value is None:  # pragma: no cover - nextval always returns a value
            raise RuntimeError("links_id_seq returned no value")
        return int(value)

    async def insert(self, link: Link) -> None:
        """Insert inside a savepoint so a duplicate code does not abort the transaction."""
        try:
            async with self._session.begin_nested():
                self._session.add(link)
        except IntegrityError as exc:
            if _violated_constraint(exc) == CODE_UNIQUE_CONSTRAINT:
                raise DuplicateCodeError(link.code) from exc
            raise

    async def get_by_code(self, code: str) -> Link | None:
        result = await self._session.execute(select(Link).where(Link.code == code))
        return result.scalar_one_or_none()
