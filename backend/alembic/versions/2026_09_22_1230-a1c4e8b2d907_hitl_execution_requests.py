"""Persist HITL execution-request metadata without secrets.

Revision ID: a1c4e8b2d907
Revises: e3a7c1f9b246
Create Date: 2026-09-22 12:30:00+00:00

R8 第二阶段：批准后立刻返回，执行请求单独记一行。

1. 新表只存提案 ID、发起人、请求 ID、尝试号、队列状态、凭据种类和时间。
   不存动态密码，也不能从这张表恢复出密码。
2. 同一提案在 queued / running / awaiting_credential 时只能有一行，
   防止重启恢复和重复点击各发一次命令。
3. 已有的 APPROVED 提案不会被回填成排队任务，避免升级后自动执行历史变更。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "a1c4e8b2d907"
down_revision: str | None = "e3a7c1f9b246"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN_PREDICATE = "status IN ('queued', 'running', 'awaiting_credential')"


def _require_destructive_downgrade() -> None:
    """删掉执行请求表会丢掉「还在排队」的恢复依据，必须显式确认。"""
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    """Create the execution-request table and its open-request uniqueness index."""
    op.create_table(
        "hitl_execution_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("credential_kind", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["hitl_proposals.id"],
            name="fk_hitl_exec_req_proposal_id_hitl_proposals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_hitl_exec_req_actor_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index(
        "ix_hitl_execution_requests_proposal_id",
        "hitl_execution_requests",
        ["proposal_id"],
    )
    op.create_index(
        "ix_hitl_execution_requests_status",
        "hitl_execution_requests",
        ["status"],
    )
    op.create_index(
        "uq_hitl_exec_req_open_proposal",
        "hitl_execution_requests",
        ["proposal_id"],
        unique=True,
        postgresql_where=sa.text(_OPEN_PREDICATE),
        sqlite_where=sa.text(_OPEN_PREDICATE),
    )


def downgrade() -> None:
    """Drop the execution-request table. Proposals and device results stay."""
    _require_destructive_downgrade()
    op.drop_index("uq_hitl_exec_req_open_proposal", table_name="hitl_execution_requests")
    op.drop_index("ix_hitl_execution_requests_status", table_name="hitl_execution_requests")
    op.drop_index("ix_hitl_execution_requests_proposal_id", table_name="hitl_execution_requests")
    op.drop_table("hitl_execution_requests")
