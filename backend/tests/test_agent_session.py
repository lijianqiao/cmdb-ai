"""Tests for app.agent.session — transcript helpers built on the CRUD layer."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.session import (
    append_assistant_message,
    append_tool_result,
    append_user_message,
    build_model_history,
)
from app.core.llm import ToolCall
from app.crud.agent_session import agent_session_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def test_build_model_history_round_trips_plain_messages(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "网段 10.0.0.0/24 有谁掉线了")
    await append_assistant_message(db_session, session_id, "让我查一下")
    await db_session.commit()

    history = await build_model_history(db_session, session_id)

    assert [m.role for m in history] == ["user", "assistant"]
    assert history[0].content == "网段 10.0.0.0/24 有谁掉线了"


async def test_build_model_history_round_trips_tool_calls(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "查一下")
    await append_assistant_message(
        db_session,
        session_id,
        "",
        tool_calls=[ToolCall(id="call_1", name="query_monitor_status", arguments="{}")],
    )
    await append_tool_result(db_session, session_id, "call_1", "10.0.0.5 离线")
    await db_session.commit()

    history = await build_model_history(db_session, session_id)

    assert history[1].tool_calls == [ToolCall(id="call_1", name="query_monitor_status", arguments="{}")]
    assert history[2].role == "tool"
    assert history[2].tool_call_id == "call_1"
    assert history[2].content == "10.0.0.5 离线"


async def test_build_model_history_respects_max_messages(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    for i in range(6):
        await append_user_message(db_session, session_id, f"msg-{i}")
    await db_session.commit()

    history = await build_model_history(db_session, session_id, max_messages=2)

    assert [m.content for m in history] == ["msg-4", "msg-5"]
