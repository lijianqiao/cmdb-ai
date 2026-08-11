"""Add durable Spawn metadata to agent_registry.

Revision ID: d6a1b4c9f235
Revises: c5f0a3b8e124
Create Date: 2026-08-11 18:30:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "d6a1b4c9f235"
down_revision: str | None = "c5f0a3b8e124"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_DEFAULTS: dict[str, int | float] = {
    "max_steps": 20,
    "max_cost_usd": 1.0,
    "max_wall_time_seconds": 120.0,
    "steps_used": 0,
    "cost_used_usd": 0.0,
}


def _require_destructive_downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    op.add_column(
        "agent_registry",
        sa.Column("trace_id", sa.String(length=36), server_default=sa.text("''"), nullable=True),
    )
    op.add_column(
        "agent_registry",
        sa.Column(
            "role_version",
            sa.String(length=30),
            server_default=sa.text("'legacy'"),
            nullable=True,
        ),
    )
    op.add_column(
        "agent_registry",
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.add_column(
        "agent_registry",
        sa.Column("force_closed", sa.Boolean(), server_default=sa.false(), nullable=True),
    )

    registry = sa.table(
        "agent_registry",
        sa.column("child_id", sa.String(length=36)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("closed_at", sa.DateTime(timezone=True)),
        sa.column("budget", sa.JSON()),
        sa.column("trace_id", sa.String(length=36)),
        sa.column("role_version", sa.String(length=30)),
        sa.column("status_changed_at", sa.DateTime(timezone=True)),
        sa.column("force_closed", sa.Boolean()),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            registry.c.child_id,
            registry.c.created_at,
            registry.c.closed_at,
            registry.c.budget,
        )
    ).mappings().all()
    update_statement = registry.update().where(
        registry.c.child_id == sa.bindparam("target_child_id")
    )
    for row in rows:
        legacy_budget = row["budget"] if isinstance(row["budget"], dict) else {}
        normalized_budget = {
            key: legacy_budget.get(key, default)
            for key, default in _BUDGET_DEFAULTS.items()
        }
        bind.execute(
            update_statement,
            {
                "target_child_id": row["child_id"],
                "trace_id": row["child_id"],
                "role_version": "legacy",
                "status_changed_at": row["closed_at"] or row["created_at"],
                "force_closed": False,
                "budget": normalized_budget,
            },
        )

    op.alter_column(
        "agent_registry",
        "trace_id",
        existing_type=sa.String(length=36),
        nullable=False,
        server_default=None,
    )
    op.alter_column(
        "agent_registry",
        "role_version",
        existing_type=sa.String(length=30),
        nullable=False,
        server_default=None,
    )
    op.alter_column(
        "agent_registry",
        "status_changed_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=None,
    )
    op.alter_column(
        "agent_registry",
        "force_closed",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    )
    op.create_index(
        "ix_agent_registry_trace_id",
        "agent_registry",
        ["trace_id"],
        unique=False,
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_index("ix_agent_registry_trace_id", table_name="agent_registry")
    op.drop_column("agent_registry", "force_closed")
    op.drop_column("agent_registry", "status_changed_at")
    op.drop_column("agent_registry", "role_version")
    op.drop_column("agent_registry", "trace_id")
