"""CRUD tests for KnowledgeCategory."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.knowledge_category import knowledge_category_crud

pytestmark = pytest.mark.asyncio


async def test_create_and_get_by_code(db_session: AsyncSession) -> None:
    category = await knowledge_category_crud.create(
        db_session, {"code": "sop", "name": "故障处理 SOP", "description": "运维故障处理手册"}
    )
    await db_session.commit()

    fetched = await knowledge_category_crud.get_by_code(db_session, "sop")
    assert fetched is not None
    assert fetched.id == category.id
    assert fetched.name == "故障处理 SOP"


async def test_get_by_code_returns_none_when_missing(db_session: AsyncSession) -> None:
    fetched = await knowledge_category_crud.get_by_code(db_session, "does-not-exist")
    assert fetched is None


async def test_list_all_orders_by_id(db_session: AsyncSession) -> None:
    first = await knowledge_category_crud.create(
        db_session, {"code": "sop", "name": "SOP", "description": ""}
    )
    await db_session.flush()
    second = await knowledge_category_crud.create(
        db_session, {"code": "topology", "name": "网络拓扑", "description": ""}
    )
    await db_session.commit()

    categories = await knowledge_category_crud.list_all(db_session)

    assert [c.id for c in categories] == [first.id, second.id]
