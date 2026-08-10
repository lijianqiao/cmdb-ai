"""Add knowledge-base tables: categories, documents, chunks (pgvector).

Revision ID: f2b6d8e1a327
Revises: e1a4c7d9f215
Create Date: 2026-08-11 10:00:00+00:00
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa

from alembic import context, op

revision: str = "f2b6d8e1a327"
down_revision: str | None = "e1a4c7d9f215"
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
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_knowledge_categories_code"), "knowledge_categories", ["code"], unique=True
    )

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("file_type", sa.String(length=20), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["category_id"], ["knowledge_categories.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_knowledge_documents_category_id"), "knowledge_documents", ["category_id"], unique=False
    )
    op.create_index(
        op.f("ix_knowledge_documents_content_hash"), "knowledge_documents", ["content_hash"], unique=False
    )
    op.create_index(
        op.f("ix_knowledge_documents_is_deleted"), "knowledge_documents", ["is_deleted"], unique=False
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(1024), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_knowledge_chunks_document_id"), "knowledge_chunks", ["document_id"], unique=False
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    op.drop_table("knowledge_categories")
