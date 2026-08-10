"""Add missing server_default to knowledge_chunks.created_at.

Revision ID: a8c3f7e29d41
Revises: f2b6d8e1a327
Create Date: 2026-08-11 11:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a8c3f7e29d41"
down_revision: str | None = "f2b6d8e1a327"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill the DB-level default that every other timestamp column has."""
    op.alter_column("knowledge_chunks", "created_at", server_default=sa.text("now()"))


def downgrade() -> None:
    """Remove the default; non-destructive, no data is lost."""
    op.alter_column("knowledge_chunks", "created_at", server_default=None)
