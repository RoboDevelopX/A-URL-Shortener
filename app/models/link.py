"""The ``links`` table: one row per short code."""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Sequence,
    String,
    Text,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.screening import SCREENING_DECISION_ENUM, ScreeningDecision

MAX_URL_LENGTH = 2048

# Explicit sequence: the service calls ``nextval`` itself to derive a generated code from the
# id *before* inserting the row.
LINKS_ID_SEQ = Sequence("links_id_seq")


class LinkStatus(enum.StrEnum):
    ACTIVE = "active"
    # Soft delete. The row stays so the code is never handed out again: reusing a code would
    # silently re-point every old copy of that link to a new destination.
    DISABLED = "disabled"


class Link(Base):
    __tablename__ = "links"
    __table_args__ = (
        CheckConstraint(f"char_length(long_url) <= {MAX_URL_LENGTH}", name="long_url_length"),
        CheckConstraint("screening_decision <> 'block'", name="never_blocked"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, LINKS_ID_SEQ, primary_key=True, server_default=LINKS_ID_SEQ.next_value()
    )
    # COLLATE "C": codes are case-sensitive (base62 uses both cases) and the btree compares
    # raw bytes, which is also the cheapest comparison. The unique constraint doubles as the
    # index for redirect lookups and is the final guard against duplicate codes.
    code: Mapped[str] = mapped_column(String(32, collation="C"), unique=True)
    long_url: Mapped[str] = mapped_column(Text)
    owner_key_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api_keys.id", ondelete="RESTRICT"), index=True
    )
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    status: Mapped[LinkStatus] = mapped_column(
        Enum(LinkStatus, name="link_status", values_callable=lambda e: [m.value for m in e]),
        default=LinkStatus.ACTIVE,
        server_default=LinkStatus.ACTIVE.value,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Denormalised from screening_events so link reads need no join. NULL means the link
    # predates screening.
    screening_decision: Mapped[ScreeningDecision | None] = mapped_column(SCREENING_DECISION_ENUM)
    screening_score: Mapped[float | None] = mapped_column(Float)
    model_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
