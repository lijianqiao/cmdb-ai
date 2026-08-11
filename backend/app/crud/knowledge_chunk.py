"""CRUD operations for knowledge-base chunks and pgvector similarity search.

Not a CRUDBase subclass: chunks are created once and never updated in place
(re-ingesting a document deletes and recreates its chunks — see
app/services/knowledge_ingestion.py), so the generic base's update/soft-delete
machinery does not apply.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument


class CRUDKnowledgeChunk:
    """Chunk persistence and pgvector cosine-distance similarity search."""

    model = KnowledgeChunk

    async def create(
        self,
        db: AsyncSession,
        *,
        document_id: int,
        chunk_index: int,
        content: str,
        token_count: int,
        embedding: list[float],
    ) -> KnowledgeChunk:
        """Add one chunk and flush."""
        chunk = KnowledgeChunk(
            document_id=document_id,
            chunk_index=chunk_index,
            content=content,
            token_count=token_count,
            embedding=embedding,
        )
        db.add(chunk)
        await db.flush()
        return chunk

    async def list_for_document(self, db: AsyncSession, document_id: int) -> list[KnowledgeChunk]:
        """Return one document's chunks ordered by chunk_index."""
        stmt = (
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == document_id)
            .order_by(KnowledgeChunk.chunk_index.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def search_similar(
        self,
        db: AsyncSession,
        *,
        query_embedding: list[float],
        category_id: int | None = None,
        top_k: int = 5,
    ) -> list[tuple[KnowledgeChunk, float]]:
        """Return the `top_k` chunks closest to `query_embedding` by cosine distance.

        Requires PostgreSQL + pgvector — `.cosine_distance()` compiles to a
        Postgres-only SQL operator and has no SQLite equivalent.
        """
        distance = KnowledgeChunk.embedding.cosine_distance(query_embedding)
        stmt = select(KnowledgeChunk, distance.label("distance")).order_by(distance).limit(top_k)
        if category_id is not None:
            stmt = stmt.join(
                KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id
            ).where(KnowledgeDocument.category_id == category_id)

        result = await db.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]


knowledge_chunk_crud = CRUDKnowledgeChunk()
