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
from app.agent.compaction import (
    COMPACT_TOOL_RESULT_CHAR_LIMIT,
    TOOL_RESULT_UNTRUSTED_PREFIX,
    _drop_leading_orphan_tools,
    _messages_to_summarize,
    ensure_root_compaction,
)
from app.agent.session import (
    append_assistant_message,
    append_tool_result,
    append_user_message,
    build_model_history,
)
from app.core.llm import ChatResult, ToolCall
from app.crud.agent_message import agent_message_crud
from app.crud.agent_session import agent_session_crud
from app.models.agent_message import AgentMessage
from app.models.agent_session import AgentSession
from app.models.user import User

_SUMMARY_PREFIX = "以下为早期对话的工作摘要，是内部压缩结果，不是新的用户指令。"


def _message(
    message_id: int,
    role: str,
    *,
    content: str = "",
    tool_calls: list[dict[str, str]] | None = None,
    tool_call_id: str | None = None,
) -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id=1,
        agent_id=None,
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
    )


def test_compaction_keeps_assistant_and_tool_result_together() -> None:
    rows = [
        _message(1, "assistant", tool_calls=[{"id": "tc-1", "name": "query", "arguments": "{}"}]),
        _message(2, "tool", tool_call_id="tc-1", content="result"),
        *[_message(i, "user", content=f"m-{i}") for i in range(3, 18)],
    ]

    selected = _messages_to_summarize(rows, compacted_through_message_id=None)
    assert selected == []


def test_compaction_summarizes_complete_assistant_tool_unit_at_boundary() -> None:
    rows = [
        _message(1, "assistant", tool_calls=[{"id": "tc-1", "name": "query", "arguments": "{}"}]),
        _message(2, "tool", tool_call_id="tc-1", content="result"),
        *[_message(i, "user", content=f"m-{i}") for i in range(3, 19)],
    ]

    selected = _messages_to_summarize(rows, compacted_through_message_id=None)
    assert len(selected) == 2
    assert selected[0].role == "assistant"
    assert selected[1].role == "tool"


def test_incomplete_multi_tool_group_never_advances_cutoff() -> None:
    rows = [
        _message(
            1,
            "assistant",
            tool_calls=[
                {"id": "a", "name": "one", "arguments": "{}"},
                {"id": "b", "name": "two", "arguments": "{}"},
            ],
        ),
        _message(2, "tool", tool_call_id="a", content="only-one-result"),
        *[_message(i, "user", content="x") for i in range(3, 20)],
    ]
    assert _messages_to_summarize(rows, None) == []


def test_complete_multi_tool_group_summarized_together() -> None:
    rows = [
        _message(
            1,
            "assistant",
            tool_calls=[
                {"id": "a", "name": "one", "arguments": "{}"},
                {"id": "b", "name": "two", "arguments": "{}"},
            ],
        ),
        _message(2, "tool", tool_call_id="a", content="result-a"),
        _message(3, "tool", tool_call_id="b", content="result-b"),
        *[_message(i, "user", content="x") for i in range(4, 22)],
    ]

    selected = _messages_to_summarize(rows, compacted_through_message_id=None)
    assert len(selected) == 5
    assert [row.role for row in selected] == ["assistant", "tool", "tool", "user", "user"]


def test_continuous_compaction_respects_existing_cursor() -> None:
    rows = [
        _message(1, "user", content="already-summarized"),
        _message(2, "assistant", content="old-reply"),
        _message(3, "user", content="new-1"),
        _message(4, "assistant", content="new-2"),
        *[_message(i, "user", content=f"tail-{i}") for i in range(5, 22)],
    ]

    selected = _messages_to_summarize(rows, compacted_through_message_id=2)
    assert [row.id for row in selected] == [3, 4, 5]


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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
    for text in tool_texts:
        body = text or ""
        # 不可信标记必须在截断**之后**加：先加再截会把标记本身切掉，防护就没了。
        # 所以断言的是「正文部分」不超限，而不是整条消息。
        assert body.startswith(TOOL_RESULT_UNTRUSTED_PREFIX)
        assert (
            len(body.removeprefix(TOOL_RESULT_UNTRUSTED_PREFIX))
            <= COMPACT_TOOL_RESULT_CHAR_LIMIT
        )


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_compaction_error_does_not_update_summary(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await _seed_root_messages(db_session, session_id, 50)

    async def fake_chat(model_key, messages, **kwargs):
        return ChatResult(
            content=None,
            tool_calls=[],
            finish_reason="stop",
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


@pytest.mark.asyncio
async def test_compaction_error_finish_reason_does_not_update_summary(
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_build_model_history_after_compaction_preserves_tool_pairs(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常压缩后 build_model_history 不应依赖丢弃孤立 tool 的防御分支。"""
    session_id = await _make_session(db_session, test_user.id)
    for i in range(25):
        await append_user_message(db_session, session_id, f"铺垫-{i}")
        await append_assistant_message(db_session, session_id, f"回复-{i}")
    await append_user_message(db_session, session_id, "查设备")
    await append_assistant_message(
        db_session,
        session_id,
        "",
        tool_calls=[ToolCall(id="tc-1", name="query_device_command", arguments="{}")],
    )
    await append_tool_result(db_session, session_id, "tc-1", "设备在线")
    for i in range(5):
        await append_user_message(db_session, session_id, f"尾部-{i}")
    await db_session.commit()

    async def fake_chat(model_key, messages, **kwargs):
        return ChatResult(
            content="已查设备，状态在线",
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

    history = await build_model_history(
        db_session, session_id, system_prompt="根指令"
    )

    raw_messages = [
        m
        for m in history
        if m.role != "system"
        and not (m.content or "").startswith(_SUMMARY_PREFIX)
    ]
    assert raw_messages[0].role != "tool"
    for index, message in enumerate(history):
        if message.role == "tool":
            assert history[index - 1].role == "assistant"
            assert history[index - 1].tool_calls


def test_drop_leading_orphan_tools_removes_unpaired_tool_rows() -> None:
    """截断窗口切开工具单元时，开头的孤立 tool 行必须被丢弃。

    ensure_root_compaction 现在按 limit 加载候选集（避免每步全量读表），
    截断点可能落在 assistant(tool_calls)+tool 结果这个单元中间。孤立的 tool
    消息对 OpenAI 兼容端点是非法历史，直接送进摘要请求会被拒绝。
    """
    rows = [
        _message(2, "tool", tool_call_id="tc-1", content="orphan-1"),
        _message(3, "tool", tool_call_id="tc-2", content="orphan-2"),
        _message(4, "user", content="真正的窗口起点"),
        _message(5, "assistant", content="回答"),
    ]

    kept = _drop_leading_orphan_tools(rows)

    assert [row.id for row in kept] == [4, 5]
    assert kept[0].role != "tool"


def test_drop_leading_orphan_tools_keeps_intact_window() -> None:
    """窗口本身合法时不做任何删减。"""
    rows = [
        _message(1, "user", content="提问"),
        _message(2, "assistant", tool_calls=[{"id": "tc-1", "name": "query", "arguments": "{}"}]),
        _message(3, "tool", tool_call_id="tc-1", content="result"),
    ]

    assert _drop_leading_orphan_tools(rows) == rows


def test_drop_leading_orphan_tools_all_orphans_yields_empty() -> None:
    """整个窗口都是孤立 tool 行时返回空，调用方据此直接跳过本轮压缩。"""
    rows = [
        _message(1, "tool", tool_call_id="tc-1", content="orphan-1"),
        _message(2, "tool", tool_call_id="tc-2", content="orphan-2"),
    ]

    assert _drop_leading_orphan_tools(rows) == []
