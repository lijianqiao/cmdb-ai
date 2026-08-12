"""Add device_command_policies table.

Revision ID: f19a7c3e6d84
Revises: b4e6f2a1c893
Create Date: 2026-08-13 10:30:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "f19a7c3e6d84"
down_revision: str | None = "b4e6f2a1c893"
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
        "device_command_policies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("asset_type", sa.String(length=50), nullable=True),
        sa.Column("asset_id", sa.Integer(), nullable=True),
        sa.Column("command_name", sa.String(length=100), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
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
        sa.ForeignKeyConstraint(["asset_id"], ["cmdb_assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_device_command_policies_is_deleted", "device_command_policies", ["is_deleted"]
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_index("ix_device_command_policies_is_deleted", table_name="device_command_policies")
    op.drop_table("device_command_policies")
