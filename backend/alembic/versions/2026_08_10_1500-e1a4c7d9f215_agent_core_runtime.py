"""Add agent-runtime core tables: sessions, messages, registry, HITL, trace events.

Revision ID: e1a4c7d9f215
Revises: d9f2b3c5a104
Create Date: 2026-08-10 15:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "e1a4c7d9f215"
down_revision: str | None = "d9f2b3c5a104"
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
        "agent_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_sessions_user_id"), "agent_sessions", ["user_id"], unique=False
    )

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_messages_session_id"), "agent_messages", ["session_id"], unique=False
    )

    op.create_table(
        "agent_registry",
        sa.Column("child_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("parent_agent_id", sa.String(length=36), nullable=True),
        sa.Column("agent_path", sa.String(length=500), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("tools_allowlist", sa.JSON(), nullable=False),
        sa.Column("sandbox_mode", sa.String(length=20), nullable=False),
        sa.Column("task_brief", sa.Text(), nullable=False),
        sa.Column("budget", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_agent_id"], ["agent_registry.child_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("child_id"),
    )
    op.create_index(
        op.f("ix_agent_registry_session_id"), "agent_registry", ["session_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_registry_parent_agent_id"),
        "agent_registry",
        ["parent_agent_id"],
        unique=False,
    )
    op.create_index(op.f("ix_agent_registry_status"), "agent_registry", ["status"], unique=False)

    op.create_table(
        "hitl_proposals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("proposed_by_agent_id", sa.String(length=36), nullable=True),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("action_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["proposed_by_agent_id"], ["agent_registry.child_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_hitl_proposals_session_id"), "hitl_proposals", ["session_id"], unique=False
    )
    op.create_index(op.f("ix_hitl_proposals_status"), "hitl_proposals", ["status"], unique=False)

    op.create_table(
        "agent_trace_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("parent_agent_id", sa.String(length=36), nullable=True),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("span_type", sa.String(length=20), nullable=False),
        sa.Column("tool", sa.String(length=100), nullable=True),
        sa.Column("control", sa.String(length=20), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error_class", sa.String(length=20), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_trace_events_trace_id"), "agent_trace_events", ["trace_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_trace_events_session_id"),
        "agent_trace_events",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_table("agent_trace_events")
    op.drop_table("hitl_proposals")
    op.drop_table("agent_registry")
    op.drop_table("agent_messages")
    op.drop_table("agent_sessions")
