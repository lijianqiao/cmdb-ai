"""Add vendor column to cmdb_assets.

Revision ID: b4e6f2a1c893
Revises: e7a3c9d1f582
Create Date: 2026-08-13 10:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "b4e6f2a1c893"
down_revision: str | None = "e7a3c9d1f582"
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
        "cmdb_assets",
        sa.Column("vendor", sa.String(length=50), nullable=False, server_default=""),
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_column("cmdb_assets", "vendor")
