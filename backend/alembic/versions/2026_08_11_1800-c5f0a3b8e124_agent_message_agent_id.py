"""Scope agent_messages to root or one spawned child.

Revision ID: c5f0a3b8e124
Revises: b3d8f1a4c672
Create Date: 2026-08-11 18:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "c5f0a3b8e124"
down_revision: str | None = "b3d8f1a4c672"
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
    op.add_column(
        "agent_messages",
        sa.Column("agent_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_agent_messages_session_id_agent_id",
        "agent_messages",
        ["session_id", "agent_id"],
        unique=False,
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_index("ix_agent_messages_session_id_agent_id", table_name="agent_messages")
    op.drop_column("agent_messages", "agent_id")
