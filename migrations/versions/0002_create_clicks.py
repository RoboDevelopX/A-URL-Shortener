"""Create the clicks table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

device_type = sa.Enum("desktop", "mobile", "tablet", "bot", "other", name="device_type")


def upgrade() -> None:
    op.create_table(
        "clicks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("link_id", sa.BigInteger(), nullable=False),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("referrer_host", sa.String(length=255), nullable=True),
        sa.Column("browser", sa.String(length=64), nullable=False),
        sa.Column("os", sa.String(length=64), nullable=False),
        sa.Column("device_type", device_type, nullable=False),
        sa.ForeignKeyConstraint(
            ["link_id"], ["links.id"], name=op.f("fk_clicks_link_id_links"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clicks")),
        sa.UniqueConstraint("event_id", name=op.f("uq_clicks_event_id")),
    )
    op.create_index("ix_clicks_link_id_clicked_at", "clicks", ["link_id", "clicked_at"])


def downgrade() -> None:
    op.drop_index("ix_clicks_link_id_clicked_at", table_name="clicks")
    op.drop_table("clicks")
    device_type.drop(op.get_bind())
