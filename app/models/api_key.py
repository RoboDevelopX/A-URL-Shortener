"""The ``api_keys`` table. Only a keyed hash of each key is stored, never the key itself."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # First characters of the key, safe to show in logs and UIs to tell keys apart.
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    # HMAC-SHA256(pepper, key) as hex. Unique, so authentication is one index lookup.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
