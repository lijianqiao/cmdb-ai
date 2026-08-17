"""Add per-turn token/cost usage columns to agent messages.

Revision ID: d0f5b8c4e236
Revises: c9e4a7b3d125
Create Date: 2026-08-17 18:00:00+00:00

四列只写在「一轮对话最终回复」那条 assistant 行上，其余行保持 NULL；
存的是整轮合计（多步循环 + 子 Agent），不是单条消息自身的开销。
全部可空，历史消息不回填——旧对话本来就没记录过用量，填 0 会假装花了 0 元。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "d0f5b8c4e236"
down_revision: str | None = "c9e4a7b3d125"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_destructive_downgrade() -> None:
    """Require an explicit opt-in before dropping columns that hold data."""
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    """Add the four nullable usage columns."""
    op.add_column("agent_messages", sa.Column("prompt_tokens", sa.Integer(), nullable=True))
    op.add_column("agent_messages", sa.Column("completion_tokens", sa.Integer(), nullable=True))
    op.add_column("agent_messages", sa.Column("cost_usd", sa.Float(), nullable=True))
    op.add_column("agent_messages", sa.Column("usage_by_model", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop the usage columns; recorded token/cost history is lost."""
    _require_destructive_downgrade()
    op.drop_column("agent_messages", "usage_by_model")
    op.drop_column("agent_messages", "cost_usd")
    op.drop_column("agent_messages", "completion_tokens")
    op.drop_column("agent_messages", "prompt_tokens")
