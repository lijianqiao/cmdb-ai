"""CRUD operations for knowledge-base document metadata."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
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


knowledge_document_crud = CRUDKnowledgeDocument()
