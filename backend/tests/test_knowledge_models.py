"""Structural tests for the knowledge-base ORM models."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_category import KnowledgeCategory
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_category_round_trip(db_session: AsyncSession) -> None:
    category = KnowledgeCategory(code="sop", name="故障处理 SOP", description="")
    db_session.add(category)
    await db_session.commit()

    stored = (
        await db_session.execute(select(KnowledgeCategory).where(KnowledgeCategory.id == category.id))
    ).scalar_one()
    assert stored.code == "sop"


async def test_document_round_trip(db_session: AsyncSession, test_user: User) -> None:
    category = KnowledgeCategory(code="sop", name="故障处理 SOP", description="")
    db_session.add(category)
    await db_session.flush()

    document = KnowledgeDocument(
        category_id=category.id,
        title="交换机重启流程",
        original_filename="reboot.md",
        file_path="sop/1_reboot.md",
        file_type="md",
        content_hash="a" * 64,
        status="processing",
        uploaded_by=test_user.id,
    )
    db_session.add(document)
    await db_session.commit()

    stored = (
        await db_session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document.id))
    ).scalar_one()
    assert stored.status == "processing"
    assert stored.is_deleted is False


async def test_chunk_stores_embedding_vector(db_session: AsyncSession, test_user: User) -> None:
    category = KnowledgeCategory(code="sop", name="故障处理 SOP", description="")
    db_session.add(category)
    await db_session.flush()
    document = KnowledgeDocument(
        category_id=category.id,
        title="",
        original_filename="a.md",
        file_path="sop/1_a.md",
        file_type="md",
        content_hash="b" * 64,
        status="processing",
        uploaded_by=test_user.id,
    )
    db_session.add(document)
    await db_session.flush()

    chunk = KnowledgeChunk(
        document_id=document.id,
        chunk_index=0,
        content="第一段内容",
        token_count=5,
        embedding=[0.1] * 1024,
    )
    db_session.add(chunk)
    await db_session.commit()

    stored = (
        await db_session.execute(select(KnowledgeChunk).where(KnowledgeChunk.id == chunk.id))
    ).scalar_one()
    assert len(stored.embedding) == 1024
    assert stored.embedding[0] == pytest.approx(0.1)
