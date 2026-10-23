"""The ``clicks`` table: one row per redirect, written asynchronously by the worker."""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Identity, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DeviceType(enum.StrEnum):
    DESKTOP = "desktop"
    MOBILE = "mobile"
    TABLET = "tablet"
    BOT = "bot"
    OTHER = "other"


class Click(Base):
    __tablename__ = "clicks"
    __table_args__ = (
        # Every stats query is "clicks of one link, optionally since a time": this composite
        # index serves all of them with a range scan.
        Index("ix_clicks_link_id_clicked_at", "link_id", "clicked_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Queue delivery is at-least-once; a unique event id turns redelivery into a no-op insert
    # (``ON CONFLICT DO NOTHING``), so each click is counted exactly once.
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    link_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("links.id", ondelete="CASCADE"))
    clicked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Only the referrer's host is kept: enough for "top referrers", and it avoids storing
    # full URLs that can carry personal data in query strings. No IP addresses are stored.
    referrer_host: Mapped[str | None] = mapped_column(String(255))
    browser: Mapped[str] = mapped_column(String(64))
    os: Mapped[str] = mapped_column(String(64))
    device_type: Mapped[DeviceType] = mapped_column(
        Enum(DeviceType, name="device_type", values_callable=lambda e: [m.value for m in e])
    )
