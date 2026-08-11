"""CRUD tests for KnowledgeChunk that don't require pgvector's SQL operators.

`search_similar()`'s actual similarity ordering needs real Postgres with the
vector extension — see test_knowledge_chunk_search_postgres.py. This file
only covers create/list, which work fine against the aiosqlite test DB
(verified: pgvector's Vector column create/insert/select all work on SQLite,
only the cosine-distance SQL operator is Postgres-only).
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_document(db_session: AsyncSession, user_id: int) -> int:
    category = await knowledge_category_crud.create(
        db_session, {"code": "sop", "name": "SOP", "description": ""}
    )
    await db_session.flush()
    document = await knowledge_document_crud.create(
        db_session,
        {
            "category_id": category.id,
            "title": "",
            "original_filename": "a.md",
            "file_path": "sop/1_a.md",
            "file_type": "md",
            "content_hash": "a" * 64,
            "status": "processing",
            "uploaded_by": user_id,
        },
    )
    await db_session.flush()
    return document.id


async def test_create_and_list_for_document_ordered_by_chunk_index(
    db_session: AsyncSession, test_user: User
) -> None:
    document_id = await _make_document(db_session, test_user.id)

    await knowledge_chunk_crud.create(
        db_session,
        document_id=document_id,
        chunk_index=1,
        content="第二段",
        token_count=3,
        embedding=[0.2] * 1024,
    )
    await knowledge_chunk_crud.create(
        db_session,
        document_id=document_id,
        chunk_index=0,
        content="第一段",
        token_count=3,
        embedding=[0.1] * 1024,
    )
    await db_session.commit()

    chunks = await knowledge_chunk_crud.list_for_document(db_session, document_id)

    assert [c.content for c in chunks] == ["第一段", "第二段"]
