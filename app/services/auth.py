"""API key issuance and verification."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import generate_api_key, hash_api_key
from app.models.api_key import ApiKey
from app.repositories.api_keys import ApiKeyRepository


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"
    default_message = "A valid X-API-Key header is required."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message, headers={"WWW-Authenticate": 'ApiKey header="X-API-Key"'})


class ApiKeyService:
    def __init__(self, session: AsyncSession, pepper: bytes) -> None:
        self._session = session
        self._repo = ApiKeyRepository(session)
        self._pepper = pepper

    async def authenticate(self, raw_key: str | None) -> ApiKey:
        """Return the active key matching ``raw_key`` or raise ``UnauthorizedError``.

        Missing, unknown and revoked keys get the same response so callers cannot probe
        which keys exist.
        """
        if not raw_key:
            raise UnauthorizedError
        api_key = await self._repo.get_active_by_hash(hash_api_key(raw_key, self._pepper))
        if api_key is None:
            raise UnauthorizedError("Invalid or revoked API key.")
        return api_key

    async def issue(self, name: str) -> tuple[str, ApiKey]:
        """Create a key and return ``(plaintext, row)``; the plaintext is not recoverable."""
        raw_key, prefix = generate_api_key()
        api_key = await self._repo.add(name, prefix, hash_api_key(raw_key, self._pepper))
        await self._session.commit()
        return raw_key, api_key

    async def revoke(self, prefix: str) -> int:
        count = await self._repo.revoke_by_prefix(prefix)
        await self._session.commit()
        return count
