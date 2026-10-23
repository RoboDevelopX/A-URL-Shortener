"""Create the links table.

Revision ID: 0001
Revises:
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

link_status = sa.Enum("active", "disabled", name="link_status")


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence("links_id_seq")))
    op.create_table(
        "links",
        sa.Column(
            "id", sa.BigInteger(), server_default=sa.text("nextval('links_id_seq')"), nullable=False
        ),
        sa.Column("code", sa.String(length=32, collation="C"), nullable=False),
        sa.Column("long_url", sa.Text(), nullable=False),
        sa.Column("is_custom", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", link_status, server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("char_length(long_url) <= 2048", name=op.f("ck_links_long_url_length")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_links")),
        sa.UniqueConstraint("code", name=op.f("uq_links_code")),
    )
    # The sequence belongs to the column, so dropping the table drops it too.
    op.execute("ALTER SEQUENCE links_id_seq OWNED BY links.id")


def downgrade() -> None:
    op.drop_table("links")
    link_status.drop(op.get_bind())
