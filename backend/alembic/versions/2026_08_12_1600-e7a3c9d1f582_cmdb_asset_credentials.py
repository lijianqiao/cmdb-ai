"""Add credential fields to cmdb_assets.

Revision ID: e7a3c9d1f582
Revises: d6a1b4c9f235
Create Date: 2026-08-12 16:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "e7a3c9d1f582"
down_revision: str | None = "d6a1b4c9f235"
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
        sa.Column("credential_type", sa.String(length=20), nullable=False, server_default="none"),
    )
    op.add_column(
        "cmdb_assets",
        sa.Column("credential_username", sa.String(length=100), nullable=False, server_default=""),
    )
    op.add_column(
        "cmdb_assets",
        sa.Column("credential_password_encrypted", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_column("cmdb_assets", "credential_password_encrypted")
    op.drop_column("cmdb_assets", "credential_username")
    op.drop_column("cmdb_assets", "credential_type")
