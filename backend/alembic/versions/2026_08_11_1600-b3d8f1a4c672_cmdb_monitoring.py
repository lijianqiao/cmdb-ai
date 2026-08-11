"""Add CMDB + monitoring tables: assets, dependencies, targets, status events.

Revision ID: b3d8f1a4c672
Revises: a8c3f7e29d41
Create Date: 2026-08-11 16:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "b3d8f1a4c672"
down_revision: str | None = "a8c3f7e29d41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_destructive_downgrade() -> None:
    """Require an explicit opt-in before removing application tables."""
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    op.create_table(
        "cmdb_assets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_type", sa.String(length=50), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=False),
        sa.Column("location", sa.String(length=200), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("business_system", sa.String(length=100), nullable=False),
        sa.Column("subnet_cidr", sa.String(length=45), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cmdb_assets_ip_address"), "cmdb_assets", ["ip_address"], unique=False)
    op.create_index(op.f("ix_cmdb_assets_is_deleted"), "cmdb_assets", ["is_deleted"], unique=False)

    op.create_table(
        "cmdb_asset_dependencies",
        sa.Column("parent_asset_id", sa.Integer(), nullable=False),
        sa.Column("child_asset_id", sa.Integer(), nullable=False),
        sa.Column("relation_type", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["parent_asset_id"], ["cmdb_assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["child_asset_id"], ["cmdb_assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("parent_asset_id", "child_asset_id"),
    )

    op.create_table(
        "monitor_targets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("cmdb_asset_id", sa.Integer(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("check_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["cmdb_asset_id"], ["cmdb_assets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_monitor_targets_cmdb_asset_id"), "monitor_targets", ["cmdb_asset_id"], unique=False
    )
    op.create_index(
        op.f("ix_monitor_targets_is_active"), "monitor_targets", ["is_active"], unique=False
    )

    op.create_table(
        "monitor_status_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["target_id"], ["monitor_targets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_monitor_status_events_target_id"),
        "monitor_status_events",
        ["target_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_monitor_status_events_checked_at"),
        "monitor_status_events",
        ["checked_at"],
        unique=False,
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_table("monitor_status_events")
    op.drop_table("monitor_targets")
    op.drop_table("cmdb_asset_dependencies")
    op.drop_table("cmdb_assets")
