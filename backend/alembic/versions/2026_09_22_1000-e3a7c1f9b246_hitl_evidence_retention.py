"""Keep HITL approval evidence independent of the chat lifecycle.

Revision ID: e3a7c1f9b246
Revises: 0dd79792bbae
Create Date: 2026-09-22 10:00:00+00:00

R4：聊天可以归档，但审批/执行证据不能跟着会话或用户被级联销毁。

1. hitl_proposals.session_id 外键 CASCADE → RESTRICT：有提案的会话删不掉；
   永久删除用户时级联到这类会话也会被挡住（服务层据此返回冲突说明原因）。
2. 新增三列，只记提案当时的事实：
   - requested_by_user_id：申请人（会话所有者）
   - approval_method：manual（人工）/ auto:<档位>（会话档位自动批准）
   - evidence_snapshot：提案当时的资产与命令快照（JSON，不含密码）
3. 回填只填能确认的：申请人取会话所有者；审批方式按审计日志里的
   hitl_approved / hitl_rejected / hitl_auto_approved 推出。历史快照不回填——
   用今天的资产值冒充当时的样子等于伪造证据，留空即「未知」。
   已经被级联删掉的旧记录迁移找不回来，只能靠备份。
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import context, op

revision: str = "e3a7c1f9b246"
down_revision: str | None = "0dd79792bbae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SESSION_FK = "fk_hitl_proposals_session_id_agent_sessions"
_REQUESTER_FK = "fk_hitl_proposals_requested_by_user_id_users"
# 最初建表时这条外键没起名，PostgreSQL 默认把它命名为 <表>_<列>_fkey
_LEGACY_SESSION_FK = "hitl_proposals_session_id_fkey"


def _require_destructive_downgrade() -> None:
    """Require an explicit opt-in before dropping evidence and restoring cascades."""
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def _session_fk_name(bind: Any) -> str:
    """找出 session_id 外键的实际名字（没起名时各库默认名不同），找不到用 PG 默认名。"""
    for foreign_key in sa.inspect(bind).get_foreign_keys("hitl_proposals"):
        if foreign_key["constrained_columns"] == ["session_id"] and foreign_key["name"]:
            return str(foreign_key["name"])
    return _LEGACY_SESSION_FK


def upgrade() -> None:
    """Protect proposals from cascades, add evidence columns, backfill confirmed facts."""
    op.drop_constraint(_session_fk_name(op.get_bind()), "hitl_proposals", type_="foreignkey")
    op.create_foreign_key(
        _SESSION_FK,
        "hitl_proposals",
        "agent_sessions",
        ["session_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column("hitl_proposals", sa.Column("requested_by_user_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        _REQUESTER_FK,
        "hitl_proposals",
        "users",
        ["requested_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "hitl_proposals", sa.Column("approval_method", sa.String(length=20), nullable=True)
    )
    op.add_column("hitl_proposals", sa.Column("evidence_snapshot", sa.JSON(), nullable=True))

    proposals = sa.table(
        "hitl_proposals",
        sa.column("id", sa.Integer),
        sa.column("session_id", sa.Integer),
        sa.column("requested_by_user_id", sa.Integer),
        sa.column("approval_method", sa.String),
    )
    sessions = sa.table("agent_sessions", sa.column("id", sa.Integer), sa.column("user_id", sa.Integer))
    audits = sa.table(
        "audit_logs",
        sa.column("action", sa.String),
        sa.column("target", sa.String),
        sa.column("detail", sa.String),
    )

    # 申请人 = 会话所有者：只有所有者能在自己的会话里发消息，这是确定的事实
    op.execute(
        proposals.update().values(
            requested_by_user_id=sa.select(sessions.c.user_id)
            .where(sessions.c.id == proposals.c.session_id)
            .scalar_subquery()
        )
    )

    # 审批方式：审计日志的 target 形如 hitl_proposal:<id>
    target = sa.literal("hitl_proposal:") + sa.cast(proposals.c.id, sa.String)

    def audited(action: str, detail_like: str | None = None) -> sa.Exists:
        conditions = [audits.c.action == action, audits.c.target == target]
        if detail_like is not None:
            conditions.append(audits.c.detail.like(detail_like))
        return sa.exists().where(*conditions)

    backfills: tuple[tuple[sa.ColumnElement[bool], str], ...] = (
        (audited("hitl_approved"), "manual"),
        (audited("hitl_rejected"), "manual"),
        (audited("hitl_auto_approved", "%审批档位：full%"), "auto:full"),
        (audited("hitl_auto_approved", "%审批档位：assist%"), "auto:assist"),
        (audited("hitl_auto_approved"), "auto"),
    )
    for condition, method in backfills:
        op.execute(
            proposals.update()
            .where(proposals.c.approval_method.is_(None), condition)
            .values(approval_method=method)
        )


def downgrade() -> None:
    """Drop evidence columns and restore the cascade; evidence recorded since is lost."""
    _require_destructive_downgrade()
    op.drop_column("hitl_proposals", "evidence_snapshot")
    op.drop_column("hitl_proposals", "approval_method")
    op.drop_constraint(_REQUESTER_FK, "hitl_proposals", type_="foreignkey")
    op.drop_column("hitl_proposals", "requested_by_user_id")
    op.drop_constraint(_SESSION_FK, "hitl_proposals", type_="foreignkey")
    op.create_foreign_key(
        _LEGACY_SESSION_FK,
        "hitl_proposals",
        "agent_sessions",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )
