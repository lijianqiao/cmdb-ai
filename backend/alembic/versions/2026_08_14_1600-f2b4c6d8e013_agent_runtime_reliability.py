"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: 2026_08_14_1600-f2b4c6d8e013_agent_runtime_reliability.py
@DateTime: 2026-08-14
@Docs: Agent 运行时可靠性 HITL 执行字段与会话 turn 租约迁移。
"""

"""Add HITL execution recovery and session turn-lease columns.

Revision ID: f2b4c6d8e013
Revises: c1a8e4b7d902
Create Date: 2026-08-14 16:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "f2b4c6d8e013"
down_revision: str | None = "c1a8e4b7d902"
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
    with op.batch_alter_table("hitl_proposals", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("status_reason", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("resolved_by_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_hitl_proposals_resolved_by_user_id_users",
            "users",
            ["resolved_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("agent_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("active_turn_token", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column("active_turn_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            op.f("ix_agent_sessions_active_turn_token"),
            ["active_turn_token"],
            unique=False,
        )


def downgrade() -> None:
    _require_destructive_downgrade()

    with op.batch_alter_table("agent_sessions", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_agent_sessions_active_turn_token"))
        batch_op.drop_column("active_turn_started_at")
        batch_op.drop_column("active_turn_token")

    with op.batch_alter_table("hitl_proposals", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_hitl_proposals_resolved_by_user_id_users",
            type_="foreignkey",
        )
        batch_op.drop_column("resolved_at")
        batch_op.drop_column("resolved_by_user_id")
        batch_op.drop_column("status_reason")
        batch_op.drop_column("execution_started_at")
