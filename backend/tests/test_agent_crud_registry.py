"""CRUD tests for AgentRegistry — the ChildReceipt store and its state machine."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_registry import InvalidAgentStatusTransitionError, agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def _spawn(db_session: AsyncSession, session_id: int) -> str:
    child = await agent_registry_crud.create(
        db_session,
        session_id=session_id,
        parent_agent_id=None,
        agent_path="/root/kb_explorer",
        role="kb_explorer",
        model="local-chat",
        tools_allowlist=["kb_grep"],
        sandbox_mode="read-only",
        task_brief="找一下重启流程",
        budget={"max_steps": 5},
    )
    await db_session.commit()
    return child.child_id


async def test_create_starts_requested(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)

    fetched = await agent_registry_crud.get(db_session, child_id)
    assert fetched is not None
    assert fetched.status == "REQUESTED"


async def test_valid_transition_chain(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)

    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")
    await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")
    updated = await agent_registry_crud.transition_status(
        db_session, child_id, "COMPLETED", result_summary="找到了,在 SOP 第 3 章"
    )
    await db_session.commit()

    assert updated.status == "COMPLETED"
    assert updated.result_summary == "找到了,在 SOP 第 3 章"


async def test_illegal_transition_raises(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)

    with pytest.raises(InvalidAgentStatusTransitionError):
        await agent_registry_crud.transition_status(db_session, child_id, "COMPLETED")


async def test_close_is_idempotent_and_force_detaches_running(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")
    await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")

    first_close = await agent_registry_crud.close(db_session, child_id)
    assert first_close.status == "CLOSED"
    assert first_close.closed_at is not None

    second_close = await agent_registry_crud.close(db_session, child_id)
    assert second_close.status == "CLOSED"
    assert second_close.closed_at == first_close.closed_at


async def test_list_active_children_excludes_closed(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    still_running = await _spawn(db_session, session_id)
    closed_one = await _spawn(db_session, session_id)
    await agent_registry_crud.close(db_session, closed_one)
    await db_session.commit()

    active = await agent_registry_crud.list_active_children(db_session, session_id)

    assert [c.child_id for c in active] == [still_running]
