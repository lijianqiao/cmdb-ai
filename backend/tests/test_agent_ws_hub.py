"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_ws_hub.py
@DateTime: 2026-08-12 12:30
@Docs: 验证 Agent WebSocket Hub 按会话隔离广播，以及 HITL 发布器只推安全摘要。
"""

from typing import Any

import pytest

from app.agent.ws_hub import AgentWsHub, WsHitlEventPublisher
from app.schemas.agent_ws import AgentWsServerMessage

pytestmark = pytest.mark.asyncio

# 与 ProposalSafeSummary 对齐的白名单字段
_SAFE_SUMMARY_KEYS = frozenset(
    {"proposal_id", "action_type", "status", "reason", "asset_id", "result_excerpt"}
)
# 原始动作载荷中不应出现在 WS 事件里的敏感键
_SENSITIVE_KEYS = frozenset({"message", "command", "command_name", "password"})


class FakeWebSocket:
    """记录 send_json 调用的假 WebSocket，供 Hub 单测使用。"""

    def __init__(self) -> None:
        """初始化空发送记录。"""
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        """保存一份快照，避免后续对象变化影响断言。"""
        self.sent.append(dict(data))


async def test_broadcast_only_reaches_same_session() -> None:
    """同 session_id 的连接收到广播，其他会话连接收不到。"""
    hub = AgentWsHub()
    ws_a1 = FakeWebSocket()
    ws_a2 = FakeWebSocket()
    ws_b = FakeWebSocket()

    await hub.connect(1, ws_a1)  # type: ignore[arg-type]
    await hub.connect(1, ws_a2)  # type: ignore[arg-type]
    await hub.connect(2, ws_b)  # type: ignore[arg-type]

    message = AgentWsServerMessage(
        type="assistant_delta",
        payload={"text": "你好", "done": False},
    )
    await hub.broadcast(1, message)

    expected = {"type": "assistant_delta", "payload": {"text": "你好", "done": False}}
    assert ws_a1.sent == [expected]
    assert ws_a2.sent == [expected]
    assert ws_b.sent == []


async def test_disconnect_stops_further_broadcast() -> None:
    """断开后的连接不再收到后续广播。"""
    hub = AgentWsHub()
    ws = FakeWebSocket()
    await hub.connect(10, ws)  # type: ignore[arg-type]
    await hub.disconnect(10, ws)  # type: ignore[arg-type]

    await hub.broadcast(
        10,
        AgentWsServerMessage(type="turn_done", payload={"reason": "completed"}),
    )
    assert ws.sent == []


async def test_ws_hitl_publisher_maps_hitl_pending_with_safe_payload() -> None:
    """WsHitlEventPublisher 将 hitl_pending 映射为 WS 消息，且载荷不含敏感键。"""
    hub = AgentWsHub()
    ws = FakeWebSocket()
    await hub.connect(42, ws)  # type: ignore[arg-type]

    publisher = WsHitlEventPublisher(hub=hub)
    # 模拟上游误把原始动作字段混入 payload；发布器必须过滤
    await publisher.publish(
        session_id=42,
        event_type="hitl_pending",
        payload={
            "proposal_id": 7,
            "action_type": "notify",
            "status": "PENDING",
            "reason": "通知运维",
            "asset_id": 3,
            "message": "含敏感通知正文",
            "command": "reboot",
            "password": "secret",
        },
    )

    assert len(ws.sent) == 1
    envelope = ws.sent[0]
    assert envelope["type"] == "hitl_pending"
    payload = envelope["payload"]
    assert set(payload.keys()) <= _SAFE_SUMMARY_KEYS
    assert payload["proposal_id"] == 7
    assert payload["action_type"] == "notify"
    assert payload["status"] == "PENDING"
    assert payload["reason"] == "通知运维"
    assert payload["asset_id"] == 3
    assert _SENSITIVE_KEYS.isdisjoint(payload.keys())


async def test_ws_hitl_publisher_maps_all_hitl_event_types() -> None:
    """hitl_resolved / hitl_execution_failed 同样走广播且只保留安全字段。"""
    hub = AgentWsHub()
    ws = FakeWebSocket()
    await hub.connect(1, ws)  # type: ignore[arg-type]
    publisher = WsHitlEventPublisher(hub=hub)

    for event_type in ("hitl_resolved", "hitl_execution_failed"):
        ws.sent.clear()
        await publisher.publish(
            session_id=1,
            event_type=event_type,
            payload={
                "proposal_id": 1,
                "action_type": "device_control",
                "status": "APPROVED",
                "reason": "端口禁用",
                "asset_id": 9,
                "command_name": "port_disable",
            },
        )
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == event_type
        assert "command" not in ws.sent[0]["payload"]
        assert "command_name" not in ws.sent[0]["payload"]
        assert set(ws.sent[0]["payload"].keys()) <= _SAFE_SUMMARY_KEYS


async def test_ws_hitl_publisher_ignores_unknown_event_type() -> None:
    """非 hitl_* 或不在 WS 契约内的事件类型不应广播。"""
    hub = AgentWsHub()
    ws = FakeWebSocket()
    await hub.connect(1, ws)  # type: ignore[arg-type]
    publisher = WsHitlEventPublisher(hub=hub)

    await publisher.publish(
        session_id=1,
        event_type="not_a_hitl_event",
        payload={"proposal_id": 1, "action_type": "notify", "status": "PENDING", "reason": "", "asset_id": None},
    )
    assert ws.sent == []
