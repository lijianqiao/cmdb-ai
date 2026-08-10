"""Tests for the standard agent loop (app.agent.loop.run_loop)."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import Budget
from app.agent.loop import LoopOutcome, ToolResult, run_loop
from app.agent.session import append_user_message, build_model_history
from app.core.llm import ChatMessage, ChatResult, ToolCall
from app.crud.agent_session import agent_session_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def _never_called_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
    raise AssertionError(f"dispatch_tool should not have been called with {name!r}")


async def test_loop_returns_final_answer_when_model_calls_no_tools(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "10.0.0.5 在线吗")
    await db_session.commit()

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(
            content="在线", tool_calls=[], finish_reason="stop", prompt_tokens=5, completion_tokens=2
        )

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=_never_called_dispatch,
        chat_fn=fake_chat,
    )
    await db_session.commit()

    assert outcome == LoopOutcome(reason="final_answer", final_answer="在线")

    history = await build_model_history(db_session, session_id)
    assert history[-1].role == "assistant"
    assert history[-1].content == "在线"


async def test_loop_dispatches_tool_and_continues_to_final_answer(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "10.0.0.5 在线吗")
    await db_session.commit()

    call_count = {"n": 0}

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ChatResult(
                content=None,
                tool_calls=[
                    ToolCall(id="call_1", name="query_monitor_status", arguments='{"ip": "10.0.0.5"}')
                ],
                finish_reason="tool_calls",
                prompt_tokens=10,
                completion_tokens=4,
            )
        return ChatResult(
            content="10.0.0.5 在线",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=15,
            completion_tokens=3,
        )

    async def fake_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        assert name == "query_monitor_status"
        assert args == {"ip": "10.0.0.5"}
        return ToolResult(control="ok", content="10.0.0.5 状态: up")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=fake_dispatch,
        chat_fn=fake_chat,
    )
    await db_session.commit()

    assert outcome.reason == "final_answer"
    assert outcome.final_answer == "10.0.0.5 在线"

    history = await build_model_history(db_session, session_id)
    roles = [m.role for m in history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert history[2].tool_call_id == "call_1"
    assert history[2].content == "10.0.0.5 状态: up"


async def test_loop_stops_early_on_pending_approval(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "重启一下 SW-12")
    await db_session.commit()

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(
            content=None,
            tool_calls=[ToolCall(id="call_1", name="propose_remediation", arguments="{}")],
            finish_reason="tool_calls",
            prompt_tokens=10,
            completion_tokens=4,
        )

    async def fake_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(control="pending_approval", content="已创建提案,等待审批")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=fake_dispatch,
        chat_fn=fake_chat,
    )

    assert outcome.reason == "early_exit"
    assert outcome.control == "pending_approval"
    assert outcome.final_answer is None


async def test_loop_backfills_skipped_tool_calls_after_early_exit_in_batch(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "批量处理一下")
    await db_session.commit()

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(
            content=None,
            tool_calls=[
                ToolCall(id="call_1", name="query_monitor_status", arguments="{}"),
                ToolCall(id="call_2", name="propose_remediation", arguments="{}"),
                ToolCall(id="call_3", name="query_monitor_status", arguments="{}"),
            ],
            finish_reason="tool_calls",
            prompt_tokens=10,
            completion_tokens=4,
        )

    async def fake_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        if name == "query_monitor_status":
            return ToolResult(control="ok", content="状态: up")
        if name == "propose_remediation":
            return ToolResult(control="pending_approval", content="已创建提案,等待审批")
        raise AssertionError(f"unexpected dispatch for {name!r}")

    dispatch_calls: list[str] = []

    async def tracking_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        dispatch_calls.append(name)
        assert len(dispatch_calls) <= 2, "3rd tool call should never be dispatched"
        return await fake_dispatch(name, args)

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=tracking_dispatch,
        chat_fn=fake_chat,
    )
    await db_session.commit()

    assert outcome.reason == "early_exit"
    assert outcome.control == "pending_approval"
    assert outcome.final_answer is None
    assert dispatch_calls == ["query_monitor_status", "propose_remediation"]

    history = await build_model_history(db_session, session_id)
    tool_messages = [m for m in history if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["call_1", "call_2", "call_3"]
    assert tool_messages[0].content == "状态: up"
    assert tool_messages[1].content == "已创建提案,等待审批"
    assert tool_messages[2].content == "已跳过：等待前一个工具调用的处理结果"


async def test_loop_stops_when_budget_exceeded(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "一直查一直查")
    await db_session.commit()

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(
            content=None,
            tool_calls=[ToolCall(id="call_x", name="query_monitor_status", arguments="{}")],
            finish_reason="tool_calls",
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def fake_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(control="ok", content="继续查")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=fake_dispatch,
        chat_fn=fake_chat,
        budget=Budget(max_steps=2, max_cost_usd=100.0),
    )

    assert outcome == LoopOutcome(reason="budget_exceeded", final_answer=None)
