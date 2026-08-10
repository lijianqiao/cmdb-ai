"""CRUD tests for KnowledgeDocument."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_category(db_session: AsyncSession, code: str = "sop") -> int:
    category = await knowledge_category_crud.create(
        db_session, {"code": code, "name": code, "description": ""}
    )
    await db_session.flush()
    return category.id


async def test_create_and_get_by_content_hash(db_session: AsyncSession, test_user: User) -> None:
    category_id = await _make_category(db_session)
    document = await knowledge_document_crud.create(
        db_session,
        {
            "category_id": category_id,
            "title": "重启流程",
            "original_filename": "reboot.md",
            "file_path": "sop/1_reboot.md",
            "file_type": "md",
            "content_hash": "a" * 64,
            "status": "processing",
            "uploaded_by": test_user.id,
        },
    )
    await db_session.commit()

    fetched = await knowledge_document_crud.get_by_content_hash(db_session, "a" * 64)
    assert fetched is not None
    assert fetched.id == document.id


async def test_get_by_content_hash_ignores_soft_deleted(
    db_session: AsyncSession, test_user: User
) -> None:
    category_id = await _make_category(db_session)
    document = await knowledge_document_crud.create(
        db_session,
        {
            "category_id": category_id,
            "title": "",
            "original_filename": "a.md",
            "file_path": "sop/1_a.md",
            "file_type": "md",
            "content_hash": "b" * 64,
            "status": "ready",
            "uploaded_by": test_user.id,
        },
    )
    await db_session.flush()
    await knowledge_document_crud.soft_delete(db_session, document.id)
    await db_session.commit()

    fetched = await knowledge_document_crud.get_by_content_hash(db_session, "b" * 64)
    assert fetched is None


async def test_list_for_category_filters_and_counts(
    db_session: AsyncSession, test_user: User
) -> None:
    sop_id = await _make_category(db_session, "sop")
    other_id = await _make_category(db_session, "topology")

    await knowledge_document_crud.create(
        db_session,
        {
            "category_id": sop_id,
            "title": "文档一",
            "original_filename": "a.md",
            "file_path": "sop/1_a.md",
            "file_type": "md",
            "content_hash": "c" * 64,
            "status": "ready",
            "uploaded_by": test_user.id,
        },
    )
    await knowledge_document_crud.create(
        db_session,
        {
            "category_id": other_id,
            "title": "文档二",
            "original_filename": "b.md",
            "file_path": "topology/2_b.md",
            "file_type": "md",
            "content_hash": "d" * 64,
            "status": "ready",
            "uploaded_by": test_user.id,
        },
    )
    await db_session.commit()

    items, total = await knowledge_document_crud.list_for_category(db_session, sop_id)

    assert total == 1
    assert items[0].title == "文档一"
