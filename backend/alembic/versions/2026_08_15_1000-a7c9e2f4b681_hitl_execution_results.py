"""Create persistent full results for HITL device queries.

Revision ID: a7c9e2f4b681
Revises: f2b4c6d8e013
Create Date: 2026-08-15 10:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "a7c9e2f4b681"
down_revision: str | None = "f2b4c6d8e013"
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
        "hitl_execution_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_length", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("summary_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("summary_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["proposal_id"], ["hitl_proposals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proposal_id"),
    )
    op.create_index(
        op.f("ix_hitl_execution_results_proposal_id"),
        "hitl_execution_results",
        ["proposal_id"],
        unique=False,
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_index(op.f("ix_hitl_execution_results_proposal_id"), table_name="hitl_execution_results")
    op.drop_table("hitl_execution_results")
