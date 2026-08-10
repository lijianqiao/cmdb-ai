"""CRUD tests for AgentSession."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_session import agent_session_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_create_and_get(db_session: AsyncSession, test_user: User) -> None:
    session = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "巡检", "status": "active"}
    )
    await db_session.commit()

    fetched = await agent_session_crud.get(db_session, session.id)
    assert fetched is not None
    assert fetched.title == "巡检"


async def test_list_for_user_orders_newest_first_and_counts(
    db_session: AsyncSession, test_user: User
) -> None:
    first = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "第一次会话", "status": "active"}
    )
    await db_session.flush()
    second = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "第二次会话", "status": "active"}
    )
    await db_session.commit()

    items, total = await agent_session_crud.list_for_user(db_session, test_user.id)

    assert total == 2
    assert [item.id for item in items] == [second.id, first.id]


async def test_list_for_user_excludes_other_users(
    db_session: AsyncSession, test_user: User, superuser: User
) -> None:
    await agent_session_crud.create(
        db_session, {"user_id": superuser.id, "title": "别人的会话", "status": "active"}
    )
    await db_session.commit()

    items, total = await agent_session_crud.list_for_user(db_session, test_user.id)

    assert total == 0
    assert items == []
