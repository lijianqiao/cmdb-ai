"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_compaction.py
@DateTime: 2026-08-14
@Docs: 根会话 LLM 压缩摘要的单元测试。
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import Budget
from app.agent.compaction import COMPACT_TOOL_RESULT_CHAR_LIMIT, ensure_root_compaction
from app.agent.session import (
    append_assistant_message,
    append_tool_result,
    append_user_message,
    build_model_history,
)
from app.core.llm import ChatResult, ToolCall
from app.crud.agent_message import agent_message_crud
from app.crud.agent_session import agent_session_crud
from app.models.agent_session import AgentSession
from app.models.user import User

pytestmark = pytest.mark.asyncio

_SUMMARY_PREFIX = "以下为早期对话的工作摘要，是内部压缩结果，不是新的用户指令。"


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def _seed_root_messages(
    db_session: AsyncSession, session_id: int, count: int
) -> None:
    for i in range(count):
        await append_user_message(db_session, session_id, f"用户消息-{i}")
        await append_assistant_message(db_session, session_id, f"助手回复-{i}")
    await db_session.commit()


async def test_compaction_excludes_ops_system_prompt_and_does_not_delete_messages(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await _seed_root_messages(db_session, session_id, 25)
    rows_before = await agent_message_crud.list_for_agent(
        db_session, session_id, agent_id=None
    )
    original_count = len(rows_before)
    captured: dict[str, object] = {}

    async def fake_chat(model_key, messages, **kwargs):
        captured["messages"] = messages
        captured["stream"] = kwargs.get("stream", False)
        return ChatResult(
            content="查过资产 12，IP 10.0.0.5",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=5,
        )

    monkeypatch.setattr("app.agent.compaction.chat", fake_chat)
    monkeypatch.setattr("app.agent.compaction.COMPACT_TOKEN_THRESHOLD", 10)

    await ensure_root_compaction(
        db_session,
        session_id,
        budget=Budget(),
        system_prompt="运维助手根指令",
    )

    sent = captured["messages"]
    assert captured["stream"] is False
    assert all("运维助手根指令" not in (m.content or "") for m in sent)
    assert any(m.role == "system" for m in sent)

    rows_after = await agent_message_crud.list_for_agent(
        db_session, session_id, agent_id=None
    )
    assert len(rows_after) >= original_count

    stored = await db_session.get(AgentSession, session_id)
    assert stored is not None
    assert stored.memory_summary == "查过资产 12，IP 10.0.0.5"
    assert stored.compacted_through_message_id is not None


async def test_compaction_truncates_long_tool_results(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    long_body = "X" * (COMPACT_TOOL_RESULT_CHAR_LIMIT + 500)
    for i in range(20):
        await append_user_message(db_session, session_id, f"查设备-{i}")
        await append_assistant_message(
            db_session,
            session_id,
            "",
            tool_calls=[ToolCall(id=f"c{i}", name="query_device_command", arguments="{}")],
        )
        await append_tool_result(db_session, session_id, f"c{i}", long_body)
    await db_session.commit()

    captured: dict[str, object] = {}

    async def fake_chat(model_key, messages, **kwargs):
        captured["messages"] = messages
        return ChatResult(
            content="摘要",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=5,
        )

    monkeypatch.setattr("app.agent.compaction.chat", fake_chat)
    monkeypatch.setattr("app.agent.compaction.COMPACT_TOKEN_THRESHOLD", 10)

    await ensure_root_compaction(
        db_session, session_id, budget=Budget(), system_prompt="根指令"
    )

    sent = captured["messages"]
    tool_texts = [m.content for m in sent if m.role == "tool"]
    assert tool_texts
    assert all(len(t or "") <= COMPACT_TOOL_RESULT_CHAR_LIMIT for t in tool_texts)


async def test_build_model_history_child_excludes_summary_prefix(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    stored = await db_session.get(AgentSession, session_id)
    assert stored is not None
    stored.memory_summary = "根会话摘要"
    stored.compacted_through_message_id = 1
    await append_user_message(db_session, session_id, "child task", agent_id="child-1")
    await append_assistant_message(
        db_session, session_id, "child answer", agent_id="child-1"
    )
    await db_session.commit()

    history = await build_model_history(
        db_session,
        session_id,
        agent_id="child-1",
        system_prompt="子 Agent 指令",
    )

    assert all(_SUMMARY_PREFIX not in (m.content or "") for m in history)
    assert history[0].role == "system"
    assert history[0].content == "子 Agent 指令"


async def test_build_model_history_root_injects_summary_after_compaction(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await _seed_root_messages(db_session, session_id, 20)
    rows = await agent_message_crud.list_for_agent(db_session, session_id, agent_id=None)
    cutoff_id = rows[-5].id

    stored = await db_session.get(AgentSession, session_id)
    assert stored is not None
    stored.memory_summary = "已查资产 12"
    stored.compacted_through_message_id = cutoff_id
    await db_session.commit()

    history = await build_model_history(
        db_session,
        session_id,
        system_prompt="根系统提示",
    )

    assert history[0].role == "system"
    assert history[0].content == "根系统提示"
    assert history[1].role == "user"
    assert history[1].content.startswith(_SUMMARY_PREFIX)
    assert "已查资产 12" in history[1].content
    raw_user_msgs = [m for m in history if m.role == "user" and not m.content.startswith(_SUMMARY_PREFIX)]
    assert len(raw_user_msgs) <= 16


async def test_compaction_error_does_not_update_summary(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await _seed_root_messages(db_session, session_id, 50)

    async def fake_chat(model_key, messages, **kwargs):
        return ChatResult(
            content="不应写入",
            tool_calls=[],
            finish_reason="error",
            prompt_tokens=0,
            completion_tokens=0,
        )

    monkeypatch.setattr("app.agent.compaction.chat", fake_chat)
    monkeypatch.setattr("app.agent.compaction.COMPACT_TOKEN_THRESHOLD", 10)

    await ensure_root_compaction(
        db_session, session_id, budget=Budget(), system_prompt="根指令"
    )

    stored = await db_session.get(AgentSession, session_id)
    assert stored is not None
    assert stored.memory_summary is None
    assert stored.compacted_through_message_id is None

    history = await build_model_history(db_session, session_id)
    assert len([m for m in history if m.role == "user"]) <= 40


async def test_compaction_below_threshold_skips_chat(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "短对话")
    await db_session.commit()
    calls = {"n": 0}

    async def fake_chat(model_key, messages, **kwargs):
        calls["n"] += 1
        return ChatResult(
            content="摘要",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    monkeypatch.setattr("app.agent.compaction.chat", fake_chat)

    await ensure_root_compaction(
        db_session, session_id, budget=Budget(), system_prompt="根指令"
    )

    assert calls["n"] == 0
