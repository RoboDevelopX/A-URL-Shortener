"""Data access for ``api_keys``."""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_key import ApiKey


class ApiKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_by_hash(self, key_hash: str) -> ApiKey | None:
        result = await self._session.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def add(self, name: str, prefix: str, key_hash: str) -> ApiKey:
        api_key = ApiKey(name=name, prefix=prefix, key_hash=key_hash)
        self._session.add(api_key)
        await self._session.flush()
        return api_key

    async def revoke_by_prefix(self, prefix: str) -> int:
        """Revoke every active key with this display prefix; returns how many were revoked."""
        result = await self._session.execute(
            update(ApiKey)
            .where(ApiKey.prefix == prefix, ApiKey.revoked_at.is_(None))
            .values(revoked_at=func.now())
            .returning(ApiKey.id)
        )
        return len(result.all())
