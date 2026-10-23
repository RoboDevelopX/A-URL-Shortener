"""Classifier decisions: an audit trail of every URL the model scored."""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ScreeningDecision(enum.StrEnum):
    ALLOW = "allow"
    REVIEW = "review"  # created, but queued for a human to look at
    BLOCK = "block"  # rejected; never becomes a link


SCREENING_DECISION_ENUM = Enum(
    ScreeningDecision, name="screening_decision", values_callable=lambda e: [m.value for m in e]
)


class ScreeningEvent(Base):
    """One row per scored URL, including blocked ones that never became links.

    This table is what makes the model operable: false-positive review reads the ``review``
    and ``block`` rows, drift monitoring compares score distributions over time, and
    ``model_version`` ties every decision to the exact artifact that made it.
    """

    __tablename__ = "screening_events"
    __table_args__ = (
        # The review queue: newest flagged decisions first, without scanning allowed rows.
        Index(
            "ix_screening_events_review_queue",
            "created_at",
            postgresql_where=text("decision IN ('review', 'block')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float)
    decision: Mapped[ScreeningDecision] = mapped_column(SCREENING_DECISION_ENUM)
    model_version: Mapped[str] = mapped_column(String(64))
    api_key_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api_keys.id", ondelete="RESTRICT")
    )
    link_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("links.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
