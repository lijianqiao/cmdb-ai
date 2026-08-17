"""Add AI category-suggestion columns to knowledge documents.

Revision ID: c9e4a7b3d125
Revises: b8d3f6a2c914
Create Date: 2026-08-17 14:00:00+00:00

建议与真实归属分开存：category_id 是文档当前所属分类，
suggested_category_id 只是待人工确认的 AI 建议。应用建议 = 把后者写进前者并清空建议。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "c9e4a7b3d125"
down_revision: str | None = "b8d3f6a2c914"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_destructive_downgrade() -> None:
    """Require an explicit opt-in before dropping columns that hold data."""
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    """Add nullable suggestion columns plus the index used by the pending-suggestion filter."""
    op.add_column(
        "knowledge_documents",
        sa.Column("suggested_category_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("suggestion_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column(
            "suggestion_reason", sa.Text(), nullable=False, server_default=""
        ),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("suggested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_knowledge_documents_suggested_category_id",
        "knowledge_documents",
        "knowledge_categories",
        ["suggested_category_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # 管理页默认按「有待确认建议」筛选，这个索引服务那个查询。
    op.create_index(
        "ix_knowledge_documents_suggested_category_id",
        "knowledge_documents",
        ["suggested_category_id"],
    )


def downgrade() -> None:
    """Drop the suggestion columns; any unapplied suggestions are lost."""
    _require_destructive_downgrade()
    op.drop_index(
        "ix_knowledge_documents_suggested_category_id",
        table_name="knowledge_documents",
    )
    op.drop_constraint(
        "fk_knowledge_documents_suggested_category_id",
        "knowledge_documents",
        type_="foreignkey",
    )
    op.drop_column("knowledge_documents", "suggested_at")
    op.drop_column("knowledge_documents", "suggestion_reason")
    op.drop_column("knowledge_documents", "suggestion_confidence")
    op.drop_column("knowledge_documents", "suggested_category_id")
