"""Add root-session compaction columns to agent_sessions.

Revision ID: c1a8e4b7d902
Revises: b9e2d4c1a856
Create Date: 2026-08-14 10:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "c1a8e4b7d902"
down_revision: str | None = "b9e2d4c1a856"
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
    op.add_column("agent_sessions", sa.Column("memory_summary", sa.Text(), nullable=True))
    op.add_column(
        "agent_sessions",
        sa.Column("compacted_through_message_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_column("agent_sessions", "compacted_through_message_id")
    op.drop_column("agent_sessions", "memory_summary")
