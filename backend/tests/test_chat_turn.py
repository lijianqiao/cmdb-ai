"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_chat_turn.py
@DateTime: 2026-08-12 12:50
@Docs: 验证 Chat turn 编排：run_loop 复用、WS 事件顺序与 HITL 安全推送。
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import ToolResult
from app.agent.ws_hub import AgentWsHub, WsHitlEventPublisher
from app.core.llm import ChatMessage, ChatResult, ToolCall
from app.crud.agent_session import agent_session_crud
from app.models.permission import Permission
from app.models.role import Role, role_permissions
from app.models.user import User

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]

_SENSITIVE_KEYS = frozenset({"message", "command", "command_name", "password", "credential"})


@pytest_asyncio.fixture(autouse=True)
async def _grant_agent_use(db_session: AsyncSession, test_role: Role) -> None:
    """自动给 test_role 挂上 agent:use——POST /agent/sessions/{id}/messages 走真实
    HTTP 路径的用例需要这个权限，跟 test_hitl_api.py::_grant_hitl_approve 同一模式。
    """
    permission = Permission(name="使用运维助手", code="agent:use", module="Agent")
    db_session.add(permission)
    await db_session.flush()
    await db_session.execute(
        role_permissions.insert().values(role_id=test_role.id, permission_id=permission.id)
    )
    await db_session.commit()


class FakeWebSocket:
    """记录 send_json 调用的假 WebSocket。"""

    def __init__(self) -> None:
        """初始化空发送记录。"""
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        """保存广播快照。"""
        self.sent.append(dict(data))


async def _make_session(db: AsyncSession, user_id: int) -> int:
    """创建测试用 Agent 会话并 flush。"""
    session = await agent_session_crud.create(
        db,
        {"user_id": user_id, "title": "chat turn", "status": "active"},
    )
    await db.flush()
    return session.id


def _event_types(ws: FakeWebSocket) -> list[str]:
    """提取已推送事件类型序列。"""
    return [item["type"] for item in ws.sent]


async def test_chat_turn_broadcasts_tool_call_then_delta_then_turn_done(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """mock chat_fn：先 tool_calls 再最终文本；WS 顺序为 tool_call → assistant_delta → turn_done。"""
    from app.agent.chat_turn import run_chat_turn

    session_id = await _make_session(db_session, test_user.id)
    test_hub = AgentWsHub()
    ws = FakeWebSocket()
    await test_hub.connect(session_id, ws)  # type: ignore[arg-type]

    call_count = {"n": 0}

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ChatResult(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="query_monitor_status",
                        arguments='{"ip_prefix": "10.0.0."}',
                    )
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
        return ToolResult(control="ok", content="10.0.0.5 状态: up")

    outcome = await run_chat_turn(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
        content="10.0.0.5 在线吗",
        chat_fn=fake_chat,
        dispatch_tool=fake_dispatch,
        hub_instance=test_hub,
    )
    await db_session.commit()

    assert outcome.reason == "final_answer"
    assert outcome.final_answer == "10.0.0.5 在线"

    types = _event_types(ws)
    assert types[0] == "tool_call"
    assert "assistant_delta" in types
    assert types[-1] == "turn_done"
    # tool_call 必须在 assistant_delta 之前
    assert types.index("tool_call") < types.index("assistant_delta")

    tool_payload = ws.sent[0]["payload"]
    assert tool_payload == {"id": "call_1", "name": "query_monitor_status"}
    assert "arguments" not in tool_payload

    delta_events = [item for item in ws.sent if item["type"] == "assistant_delta"]
    assert delta_events[-1]["payload"]["done"] is True
    assert "".join(item["payload"]["text"] for item in delta_events) == "10.0.0.5 在线"

    turn_done = ws.sent[-1]
    assert turn_done["payload"]["reason"] == "final_answer"


async def test_chat_turn_forwards_on_delta_tokens_then_done(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """chat_fn 调用 on_delta 时逐片推送 assistant_delta，最后补 done=true 空片。"""
    from app.agent.chat_turn import run_chat_turn

    session_id = await _make_session(db_session, test_user.id)
    test_hub = AgentWsHub()
    ws = FakeWebSocket()
    await test_hub.connect(session_id, ws)  # type: ignore[arg-type]

    async def streaming_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        on_delta = kwargs.get("on_delta")
        assert kwargs.get("stream") is True
        assert on_delta is not None
        await on_delta("片")
        await on_delta("段")
        return ChatResult(
            content="片段",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=3,
            completion_tokens=2,
        )

    async def unused_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError(f"不应调用工具: {name}")

    outcome = await run_chat_turn(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
        content="流式测试",
        chat_fn=streaming_chat,
        dispatch_tool=unused_dispatch,
        hub_instance=test_hub,
    )
    await db_session.commit()

    assert outcome.reason == "final_answer"
    assert outcome.final_answer == "片段"

    delta_events = [item for item in ws.sent if item["type"] == "assistant_delta"]
    assert [item["payload"] for item in delta_events] == [
        {"text": "片", "done": False},
        {"text": "段", "done": False},
        {"text": "", "done": True},
    ]
    assert ws.sent[-1]["type"] == "turn_done"


async def test_chat_turn_pending_approval_emits_hitl_pending_without_secrets(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """pending_approval 时由 publisher 推 hitl_pending，且不含敏感键。"""
    from app.agent.chat_turn import run_chat_turn

    session_id = await _make_session(db_session, test_user.id)
    test_hub = AgentWsHub()
    ws = FakeWebSocket()
    await test_hub.connect(session_id, ws)  # type: ignore[arg-type]
    publisher = WsHitlEventPublisher(hub=test_hub)

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call_hitl",
                    name="propose_device_control",
                    arguments='{"asset_id": 1, "command_name": "reboot", "reason": "重启设备"}',
                )
            ],
            finish_reason="tool_calls",
            prompt_tokens=8,
            completion_tokens=3,
        )

    async def fake_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        assert name == "propose_device_control"
        await publisher.publish(
            session_id=session_id,
            event_type="hitl_pending",
            payload={
                "proposal_id": 99,
                "action_type": "device_control",
                "status": "PENDING",
                "reason": "重启设备",
                "asset_id": 1,
                "command_name": "reboot",
                "password": "s3cret",
                "message": "不得出现在 WS",
            },
        )
        return ToolResult(control="pending_approval", content="等待人工审批 #99")

    outcome = await run_chat_turn(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
        content="重启 SW-12",
        chat_fn=fake_chat,
        dispatch_tool=fake_dispatch,
        hub_instance=test_hub,
        publisher=publisher,
    )
    await db_session.commit()

    assert outcome.reason == "early_exit"
    assert outcome.control == "pending_approval"

    types = _event_types(ws)
    assert "tool_call" in types
    assert "hitl_pending" in types
    assert types[-1] == "turn_done"
    assert types.index("tool_call") < types.index("hitl_pending")
    assert types.index("hitl_pending") < types.index("turn_done")

    hitl = next(item for item in ws.sent if item["type"] == "hitl_pending")
    assert hitl["payload"]["proposal_id"] == 99
    assert _SENSITIVE_KEYS.isdisjoint(hitl["payload"].keys())

    turn_done = ws.sent[-1]["payload"]
    assert turn_done["reason"] == "early_exit"
    assert turn_done["control"] == "pending_approval"


async def test_chat_turn_error_broadcasts_chinese_message_and_keeps_user_message(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """chat_fn 抛错时推送中文 error，且用户消息仍可提交。"""
    from app.agent.chat_turn import run_chat_turn
    from app.agent.session import build_model_history

    session_id = await _make_session(db_session, test_user.id)
    test_hub = AgentWsHub()
    ws = FakeWebSocket()
    await test_hub.connect(session_id, ws)  # type: ignore[arg-type]

    async def boom_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        raise RuntimeError("upstream timeout")

    async def unused_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError("dispatch 不应在 chat 失败前被调用")

    with pytest.raises(RuntimeError, match="upstream timeout"):
        await run_chat_turn(
            db_session,
            session_id=session_id,
            actor_user_id=test_user.id,
            content="会失败的问题",
            chat_fn=boom_chat,
            dispatch_tool=unused_dispatch,
            hub_instance=test_hub,
        )

    # 编排层不 commit；调用方应仍能 commit 已写入的用户消息
    await db_session.commit()

    types = _event_types(ws)
    assert "error" in types
    err = next(item for item in ws.sent if item["type"] == "error")
    assert "message" in err["payload"]
    assert "upstream timeout" not in err["payload"]["message"]
    assert "Traceback" not in err["payload"]["message"]

    history = await build_model_history(db_session, session_id)
    assert any(m.role == "user" and m.content == "会失败的问题" for m in history)


async def test_post_messages_endpoint_uses_chat_turn(
    client: AsyncClient,
    auth_headers: Headers,
    test_user: User,
    db_session: AsyncSession,
) -> None:
    """POST /agent/sessions/{id}/messages 触发编排并返回 turn 结果。"""
    from app.agent.loop import LoopOutcome

    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "api turn", "status": "active"},
    )
    await db_session.commit()

    with patch(
        "app.api.v1.agent_sessions.run_chat_turn",
        new_callable=AsyncMock,
        return_value=LoopOutcome(
            reason="final_answer",
            final_answer="你好，我是运维助手",
        ),
    ) as mocked_turn:
        response = await client.post(
            f"/api/v1/agent/sessions/{session.id}/messages",
            json={"content": "你好"},
            headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["reason"] == "final_answer"
    assert data["final_answer"] == "你好，我是运维助手"
    mocked_turn.assert_awaited_once()
    kwargs = mocked_turn.await_args.kwargs
    assert kwargs["session_id"] == session.id
    assert kwargs["actor_user_id"] == test_user.id
    assert kwargs["content"] == "你好"


async def test_chat_turn_passes_db_to_default_chat(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 chat 路径应向 llm.chat 传入 db 会话。"""
    from app.agent import chat_turn as chat_turn_module
    from app.agent.chat_turn import run_chat_turn

    session_id = await _make_session(db_session, test_user.id)
    test_hub = AgentWsHub()
    ws = FakeWebSocket()
    await test_hub.connect(session_id, ws)  # type: ignore[arg-type]

    captured: dict[str, Any] = {}

    async def spy_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        captured["db"] = kwargs.get("db")
        return ChatResult(
            content="你好",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    monkeypatch.setattr(chat_turn_module, "chat", spy_chat)

    outcome = await run_chat_turn(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
        content="你好",
        hub_instance=test_hub,
    )
    await db_session.commit()

    assert outcome.reason == "final_answer"
    assert captured["db"] is db_session


async def test_chat_turn_injected_chat_fn_does_not_receive_db(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """注入的 chat_fn 不应被迫接受 db 关键字。"""
    from app.agent.chat_turn import run_chat_turn

    session_id = await _make_session(db_session, test_user.id)
    test_hub = AgentWsHub()
    ws = FakeWebSocket()
    await test_hub.connect(session_id, ws)  # type: ignore[arg-type]

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        assert "db" not in kwargs
        return ChatResult(
            content="你好",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    outcome = await run_chat_turn(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
        content="你好",
        chat_fn=fake_chat,
        hub_instance=test_hub,
    )
    await db_session.commit()

    assert outcome.reason == "final_answer"
