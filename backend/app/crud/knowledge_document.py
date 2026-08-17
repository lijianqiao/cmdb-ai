"""CRUD operations for knowledge-base document metadata."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase, contains_pattern
from app.models.knowledge_document import KnowledgeDocument


class CRUDKnowledgeDocument(CRUDBase[KnowledgeDocument]):
    """Knowledge document metadata persistence.

    Generic get/create/update/soft_delete come from CRUDBase — this model
    has an `is_deleted` column, so soft-delete works without an override.
    """

    model = KnowledgeDocument

    async def get_by_content_hash(
        self, db: AsyncSession, content_hash: str
    ) -> KnowledgeDocument | None:
        """Return one active document by its content hash (dedup check), or None."""
        stmt = self._active_statement().where(KnowledgeDocument.content_hash == content_hash)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_category(
        self,
        db: AsyncSession,
        category_id: int,
        *,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[KnowledgeDocument], int]:
        """Return one category's active documents newest-first with a total count."""
        count_stmt = self._active_statement().where(
            KnowledgeDocument.category_id == category_id
        )
        total = (
            await db.execute(select(func.count()).select_from(count_stmt.subquery()))
        ).scalar_one()

        stmt = (
            self._active_statement()
            .where(KnowledgeDocument.category_id == category_id)
            .order_by(KnowledgeDocument.id.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all()), total


    async def list_filtered(
        self,
        db: AsyncSession,
        *,
        category_id: int | None = None,
        search: str | None = None,
        pending_suggestion: bool | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[KnowledgeDocument], int]:
        """分页列出活跃文档，供知识库管理页使用。

        Args:
            db: 数据库会话。
            category_id: 按当前所属分类筛选。
            search: 对标题与原始文件名做模糊匹配。
            pending_suggestion: True 只看有待确认 AI 建议的；False 只看没有的。
            skip: 分页偏移。
            limit: 每页条数。

        Returns:
            文档列表（新的在前）与符合条件的总数。
        """
        stmt = self._active_statement()
        if category_id is not None:
            stmt = stmt.where(KnowledgeDocument.category_id == category_id)
        if search:
            pattern = contains_pattern(search)
            stmt = stmt.where(
                KnowledgeDocument.title.ilike(pattern, escape="\\")
                | KnowledgeDocument.original_filename.ilike(pattern, escape="\\")
            )
        if pending_suggestion is True:
            stmt = stmt.where(KnowledgeDocument.suggested_category_id.is_not(None))
        elif pending_suggestion is False:
            stmt = stmt.where(KnowledgeDocument.suggested_category_id.is_(None))

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await db.execute(count_stmt)).scalar_one()

        page_stmt = stmt.order_by(KnowledgeDocument.id.desc()).offset(skip).limit(limit)
        rows = list((await db.execute(page_stmt)).scalars().all())
        return rows, total

    async def list_by_ids(
        self, db: AsyncSession, document_ids: list[int]
    ) -> list[KnowledgeDocument]:
        """按 ID 批量返回活跃文档；不存在或已删除的 ID 直接缺席。"""
        if not document_ids:
            return []
        stmt = (
            self._active_statement()
            .where(KnowledgeDocument.id.in_(document_ids))
            .order_by(KnowledgeDocument.id.asc())
        )
        return list((await db.execute(stmt)).scalars().all())

    async def save_suggestion(
        self,
        db: AsyncSession,
        document: KnowledgeDocument,
        *,
        suggested_category_id: int | None,
        confidence: float | None,
        reason: str,
    ) -> KnowledgeDocument:
        """写入一条 AI 分类建议，不改变文档当前归属。"""
        document.suggested_category_id = suggested_category_id
        document.suggestion_confidence = confidence
        document.suggestion_reason = reason
        document.suggested_at = datetime.now(UTC)
        await db.flush()
        return document

    async def apply_category(
        self,
        db: AsyncSession,
        document: KnowledgeDocument,
        category_id: int,
    ) -> KnowledgeDocument:
        """把文档归到指定分类，并清空已消费的建议。

        建议一旦被采纳或被人工覆盖就没有保留价值——留着会让管理页反复提示
        「有待确认建议」。所以这里无条件清空，不区分是采纳还是覆盖。
        """
        document.category_id = category_id
        document.suggested_category_id = None
        document.suggestion_confidence = None
        document.suggestion_reason = ""
        document.suggested_at = None
        await db.flush()
        return document

    async def count_by_category(self, db: AsyncSession) -> dict[int, int]:
        """返回每个分类下的活跃文档数，供管理页侧栏显示。"""
        stmt = (
            select(KnowledgeDocument.category_id, func.count())
            .where(KnowledgeDocument.is_deleted.is_(False))
            .group_by(KnowledgeDocument.category_id)
        )
        return {row[0]: row[1] for row in (await db.execute(stmt)).all()}


knowledge_document_crud = CRUDKnowledgeDocument()
