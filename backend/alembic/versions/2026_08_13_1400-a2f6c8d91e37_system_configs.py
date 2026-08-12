"""Add system_configs table.

Revision ID: a2f6c8d91e37
Revises: f19a7c3e6d84
Create Date: 2026-08-13 14:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "a2f6c8d91e37"
down_revision: str | None = "f19a7c3e6d84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_destructive_downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    op.create_table(
        "system_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_system_configs_key", "system_configs", ["key"], unique=True
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_index("ix_system_configs_key", table_name="system_configs")
    op.drop_table("system_configs")
