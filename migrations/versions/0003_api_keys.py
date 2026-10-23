"""Add API keys and link ownership.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23

``links.owner_key_id`` must be NOT NULL, but links created before this migration have no
owner. Adding a NOT NULL column to a populated table fails, so this follows the usual
expand -> backfill -> contract sequence: add the column as nullable, assign existing rows
to a placeholder key that is already revoked (so nobody can act as it), then tighten the
column to NOT NULL.
"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
    )
    op.create_index(op.f("ix_api_keys_prefix"), "api_keys", ["prefix"])

    op.add_column("links", sa.Column("owner_key_id", sa.BigInteger(), nullable=True))
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM links)")).scalar():
        # The hash is of random bytes nobody knows, and the key is revoked: unusable.
        placeholder_id = conn.execute(
            sa.text(
                "INSERT INTO api_keys (name, prefix, key_hash, revoked_at) "
                "VALUES ('pre-auth links', 'legacy', :key_hash, now()) RETURNING id"
            ),
            {"key_hash": secrets.token_hex(32)},
        ).scalar_one()
        conn.execute(
            sa.text("UPDATE links SET owner_key_id = :owner WHERE owner_key_id IS NULL"),
            {"owner": placeholder_id},
        )
    op.alter_column("links", "owner_key_id", nullable=False)
    op.create_index(op.f("ix_links_owner_key_id"), "links", ["owner_key_id"])
    op.create_foreign_key(
        op.f("fk_links_owner_key_id_api_keys"),
        "links",
        "api_keys",
        ["owner_key_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_links_owner_key_id_api_keys"), "links", type_="foreignkey")
    op.drop_index(op.f("ix_links_owner_key_id"), table_name="links")
    op.drop_column("links", "owner_key_id")
    op.drop_index(op.f("ix_api_keys_prefix"), table_name="api_keys")
    op.drop_table("api_keys")
