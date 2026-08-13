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
    assert history[2].content.endswith("10.0.0.5 状态: up")


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
    assert tool_messages[0].content.endswith("状态: up")
    assert tool_messages[1].content.endswith("已创建提案,等待审批")
    assert tool_messages[2].content.endswith("已跳过：等待前一个工具调用的处理结果")


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


async def test_child_loop_never_reads_or_writes_root_transcript(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "root-only-message")
    await append_user_message(
        db_session, session_id, "child-only-task", agent_id="child-1"
    )
    await db_session.commit()

    async def fake_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        assert model_key == "local-chat"
        assert [message.content for message in messages] == [
            "child system",
            "child-only-task",
        ]
        return ChatResult(
            content="child-only-answer",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=2,
            completion_tokens=1,
        )

    async def no_tools(name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError(f"unexpected tool {name!r} with {args!r}")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        agent_id="child-1",
        system_prompt="child system",
        model_key="local-chat",
        dispatch_tool=no_tools,
        chat_fn=fake_chat,
    )
    await db_session.commit()

    root_history = await build_model_history(db_session, session_id)
    child_history = await build_model_history(
        db_session, session_id, agent_id="child-1"
    )
    assert outcome.final_answer == "child-only-answer"
    assert [message.content for message in root_history] == ["root-only-message"]
    assert [message.content for message in child_history] == [
        "child-only-task",
        "child-only-answer",
    ]


async def test_child_loop_reinjects_one_system_prompt_on_every_model_iteration(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "inspect", agent_id="child-1")
    seen: list[list[tuple[str, str]]] = []

    async def fake_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        seen.append([(item.role, item.content) for item in messages])
        if len(seen) == 1:
            return ChatResult(
                content=None,
                tool_calls=[ToolCall(id="call-1", name="kb_read", arguments="{}")],
                finish_reason="tool_calls",
                prompt_tokens=1,
                completion_tokens=1,
            )
        return ChatResult(
            content="done",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(control="ok", content="evidence")

    await run_loop(
        db_session,
        session_id=session_id,
        agent_id="child-1",
        system_prompt="owned instructions",
        model_key="local-chat",
        dispatch_tool=dispatch,
        chat_fn=fake_chat,
    )

    assert len(seen) == 2
    assert all(history[0] == ("system", "owned instructions") for history in seen)
    assert all(sum(role == "system" for role, _ in history) == 1 for history in seen)


async def test_loop_keeps_final_answer_that_crosses_cost_budget(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "root-only-message")
    await append_user_message(
        db_session, session_id, "expensive question", agent_id="child-1"
    )
    await db_session.commit()
    seen_messages: list[list[tuple[str, str]]] = []

    async def expensive_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        seen_messages.append([(message.role, message.content) for message in messages])
        return ChatResult(
            content="answer already incurred cost",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.60,
        )

    async def no_tools(name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError(f"unexpected tool {name!r} with {args!r}")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        agent_id="child-1",
        system_prompt="child system",
        model_key="local-chat",
        dispatch_tool=no_tools,
        budget=Budget(max_steps=2, max_cost_usd=0.50),
        chat_fn=expensive_chat,
    )

    assert outcome == LoopOutcome(
        reason="final_answer", final_answer="answer already incurred cost"
    )
    root_history = await build_model_history(db_session, session_id)
    child_history = await build_model_history(db_session, session_id, agent_id="child-1")
    assert seen_messages == [[("system", "child system"), ("user", "expensive question")]]
    assert [message.content for message in root_history] == ["root-only-message"]
    assert [message.content for message in child_history] == [
        "expensive question",
        "answer already incurred cost",
    ]


async def test_loop_does_not_execute_tool_calls_after_cost_budget_is_crossed(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "expensive lookup")
    await db_session.commit()
    dispatched = False

    async def expensive_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        return ChatResult(
            content=None,
            tool_calls=[ToolCall(id="call-1", name="kb_read", arguments="{}")],
            finish_reason="tool_calls",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.60,
        )

    async def must_not_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        nonlocal dispatched
        dispatched = True
        return ToolResult(control="ok", content="unexpected")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=must_not_dispatch,
        budget=Budget(max_steps=2, max_cost_usd=0.50),
        chat_fn=expensive_chat,
    )

    assert outcome == LoopOutcome(reason="budget_exceeded", final_answer=None)
    assert dispatched is False


async def test_run_loop_injected_chat_fn_does_not_receive_db(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "你好")
    await db_session.commit()

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        assert "db" not in kwargs
        return ChatResult(
            content="ok",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=_never_called_dispatch,
        chat_fn=fake_chat,
    )
    await db_session.commit()

    assert outcome.reason == "final_answer"


async def test_run_loop_uses_database_config_for_default_chat(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import httpx

    from app.core.data_encryption import encrypt_secret
    from app.core.llm import ModelConfig
    from app.crud.system_config import system_config_crud

    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "你好")
    await db_session.commit()

    encrypted = encrypt_secret("loop-db-chat-key")
    await system_config_crud.upsert_values(
        db_session,
        {
            "LLM_CHAT_BASE_URL": "https://loop-db-chat.example/v1",
            "LLM_CHAT_API_KEY": encrypted,
            "LLM_CHAT_MODEL": "loop-db-chat-model",
        },
        updated_by_user_id=None,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(
            "https://loop-db-chat.example/v1/chat/completions"
        )
        assert request.headers["Authorization"] == "Bearer loop-db-chat-key"
        assert json.loads(request.content)["model"] == "loop-db-chat-model"
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {"content": "ok", "tool_calls": []},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    def fake_build_client(config: ModelConfig) -> httpx.AsyncClient:
        headers = (
            {"Authorization": f"Bearer {config.api_key}"}
            if config.api_key
            else {}
        )
        return httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            timeout=config.timeout_seconds,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("app.core.llm._build_client", fake_build_client)

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=_never_called_dispatch,
    )
    await db_session.commit()

    assert outcome.reason == "final_answer"
    assert outcome.final_answer == "ok"
