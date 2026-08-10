"""CRUD operations for knowledge-base categories."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.knowledge_category import KnowledgeCategory


class CRUDKnowledgeCategory(CRUDBase[KnowledgeCategory]):
    """Knowledge category persistence; generic get/create/update come from CRUDBase."""

    model = KnowledgeCategory

    async def get_by_code(self, db: AsyncSession, code: str) -> KnowledgeCategory | None:
        """Return one category by its unique code, or None."""
        stmt = select(KnowledgeCategory).where(KnowledgeCategory.code == code)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self, db: AsyncSession) -> list[KnowledgeCategory]:
        """Return every category, ordered by id."""
        stmt = select(KnowledgeCategory).order_by(KnowledgeCategory.id.asc())
        result = await db.execute(stmt)
        return list(result.scalars().all())


knowledge_category_crud = CRUDKnowledgeCategory()
