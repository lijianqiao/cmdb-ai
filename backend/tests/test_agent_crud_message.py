"""CRUD tests for AgentMessage (append-only transcript storage)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_message import agent_message_crud
from app.crud.agent_session import agent_session_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def test_append_and_list_preserves_order(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)

    await agent_message_crud.append(db_session, session_id=session_id, role="user", content="在吗")
    await agent_message_crud.append(
        db_session, session_id=session_id, role="assistant", content="在的"
    )
    await db_session.commit()

    messages = await agent_message_crud.list_for_session(db_session, session_id)

    assert [m.content for m in messages] == ["在吗", "在的"]
    assert [m.role for m in messages] == ["user", "assistant"]


async def test_append_stores_tool_calls_and_tool_call_id(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)

    await agent_message_crud.append(
        db_session,
        session_id=session_id,
        role="assistant",
        content="",
        tool_calls=[{"id": "call_1", "name": "kb_grep", "arguments": "{}"}],
    )
    await agent_message_crud.append(
        db_session,
        session_id=session_id,
        role="tool",
        content="没找到匹配",
        tool_call_id="call_1",
    )
    await db_session.commit()

    messages = await agent_message_crud.list_for_session(db_session, session_id)

    assert messages[0].tool_calls == [{"id": "call_1", "name": "kb_grep", "arguments": "{}"}]
    assert messages[1].tool_call_id == "call_1"


async def test_list_for_session_limit_keeps_most_recent(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    for i in range(5):
        await agent_message_crud.append(
            db_session, session_id=session_id, role="user", content=f"msg-{i}"
        )
    await db_session.commit()

    messages = await agent_message_crud.list_for_session(db_session, session_id, limit=2)

    assert [m.content for m in messages] == ["msg-3", "msg-4"]


async def test_list_for_session_limit_zero_returns_empty(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    for i in range(3):
        await agent_message_crud.append(
            db_session, session_id=session_id, role="user", content=f"msg-{i}"
        )
    await db_session.commit()

    messages = await agent_message_crud.list_for_session(db_session, session_id, limit=0)

    assert messages == []


async def test_list_for_agent_isolates_root_and_two_children(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await agent_message_crud.append(
        db_session, session_id=session_id, agent_id=None, role="user", content="root"
    )
    await agent_message_crud.append(
        db_session, session_id=session_id, agent_id="child-a", role="user", content="a"
    )
    await agent_message_crud.append(
        db_session, session_id=session_id, agent_id="child-b", role="user", content="b"
    )
    await db_session.commit()

    root = await agent_message_crud.list_for_agent(db_session, session_id, agent_id=None)
    child_a = await agent_message_crud.list_for_agent(
        db_session, session_id, agent_id="child-a"
    )
    all_rows = await agent_message_crud.list_for_session(db_session, session_id)

    assert [row.content for row in root] == ["root"]
    assert [row.content for row in child_a] == ["a"]
    assert [row.content for row in all_rows] == ["root", "a", "b"]
