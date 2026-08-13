"""Add approval_mode column to agent_sessions.

Revision ID: b9e2d4c1a856
Revises: a2f6c8d91e37
Create Date: 2026-08-13 18:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "b9e2d4c1a856"
down_revision: str | None = "a2f6c8d91e37"
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
        "agent_sessions",
        sa.Column(
            "approval_mode",
            sa.String(length=20),
            nullable=False,
            server_default="ask",
        ),
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_column("agent_sessions", "approval_mode")
