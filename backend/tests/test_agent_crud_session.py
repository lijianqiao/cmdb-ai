"""CRUD tests for AgentSession."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_session import agent_session_crud
from app.models.agent_session import AgentSession
from app.models.user import User

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def agent_session(db_session: AsyncSession, test_user: User) -> AgentSession:
    """创建一条测试用 Agent 会话。"""
    session = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "租约测试", "status": "active"}
    )
    await db_session.commit()
    return session


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


async def test_turn_lease_is_owner_token_guarded(db_session, agent_session) -> None:
    assert await agent_session_crud.claim_turn(db_session, agent_session.id, "token-a")
    assert not await agent_session_crud.claim_turn(db_session, agent_session.id, "token-b")
    assert not await agent_session_crud.release_turn(db_session, agent_session.id, "token-b")
    assert await agent_session_crud.release_turn(db_session, agent_session.id, "token-a")


async def test_recover_active_turns_clears_non_empty_leases(
    db_session: AsyncSession, test_user: User
) -> None:
    """启动恢复只清空遗留的非空租约，不影响空闲会话。"""
    busy = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "忙", "status": "active"}
    )
    idle = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "闲", "status": "active"}
    )
    await db_session.commit()
    assert await agent_session_crud.claim_turn(db_session, busy.id, "stale-token")
    await db_session.commit()

    cleared = await agent_session_crud.recover_active_turns(db_session)
    await db_session.commit()

    assert cleared == 1
    refreshed_busy = await agent_session_crud.get(db_session, busy.id)
    refreshed_idle = await agent_session_crud.get(db_session, idle.id)
    assert refreshed_busy is not None
    assert refreshed_busy.active_turn_token is None
    assert refreshed_busy.active_turn_started_at is None
    assert refreshed_idle is not None
    assert refreshed_idle.active_turn_token is None
