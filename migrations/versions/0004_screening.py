"""Add ML screening: decisions on links and the screening_events audit table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23

The new link columns are nullable on purpose: NULL means "created before screening
existed", which is true for any existing row, so no backfill is needed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

screening_decision = sa.Enum("allow", "review", "block", name="screening_decision")


def upgrade() -> None:
    op.create_table(
        "screening_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("decision", screening_decision, nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("api_key_id", sa.BigInteger(), nullable=False),
        sa.Column("link_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_screening_events_api_key_id_api_keys"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["link_id"],
            ["links.id"],
            name=op.f("fk_screening_events_link_id_links"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screening_events")),
    )
    op.create_index(
        "ix_screening_events_review_queue",
        "screening_events",
        ["created_at"],
        postgresql_where=sa.text("decision IN ('review', 'block')"),
    )
    op.add_column("links", sa.Column("screening_decision", screening_decision, nullable=True))
    op.add_column("links", sa.Column("screening_score", sa.Float(), nullable=True))
    op.add_column("links", sa.Column("model_version", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        op.f("ck_links_never_blocked"), "links", "screening_decision <> 'block'"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_links_never_blocked"), "links", type_="check")
    op.drop_column("links", "model_version")
    op.drop_column("links", "screening_score")
    op.drop_column("links", "screening_decision")
    op.drop_index("ix_screening_events_review_queue", table_name="screening_events")
    op.drop_table("screening_events")
    screening_decision.drop(op.get_bind())
