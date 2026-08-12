"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: ws_hub.py
@DateTime: 2026-08-12 12:30
@Docs: 进程内 Agent WebSocket 连接 Hub，以及将 HITL 事件映射为安全 WS 推送的发布器。

实现流程：
1. AgentWsHub 用 session_id → WebSocket 集合管理连接；broadcast 只发给同一会话的连接。
2. 发送失败或已断开的连接从集合中剔除，避免断线后反复报错（路由层仍应主动 disconnect）。
3. WsHitlEventPublisher 实现 T10 HitlEventPublisher Protocol：只接受 hitl_* 事件类型，
   并把 payload 过滤到 ProposalSafeSummary 白名单后再 broadcast。
4. 模块级 hub 单例供后续 API / chat_turn / HITL 注入，本任务不挂 HTTP/WS 路由。
"""

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, cast

from starlette.websockets import WebSocket, WebSocketState

from app.schemas.agent_ws import AgentWsEventType, AgentWsServerMessage

# 与 T10 ProposalSafeSummary 字段对齐；绝不透传原始动作载荷
_HITL_SAFE_KEYS = frozenset({"proposal_id", "action_type", "status", "reason", "asset_id"})
_HITL_EVENT_TYPES = frozenset({"hitl_pending", "hitl_resolved", "hitl_execution_failed"})


class AgentWsHub:
    """进程内按会话隔离的 WebSocket 连接表与广播器。"""

    def __init__(self) -> None:
        """初始化空连接表。"""
        self._connections: dict[int, set[WebSocket]] = defaultdict(set)

    async def connect(self, session_id: int, websocket: WebSocket) -> None:
        """
        将 WebSocket 注册到指定会话。

        Args:
            session_id: Agent 会话主键
            websocket: 已接受的 WebSocket 连接
        """
        self._connections[session_id].add(websocket)

    async def disconnect(self, session_id: int, websocket: WebSocket) -> None:
        """
        从指定会话注销 WebSocket；集合为空时清理键。

        Args:
            session_id: Agent 会话主键
            websocket: 要移除的连接
        """
        peers = self._connections.get(session_id)
        if peers is None:
            return
        peers.discard(websocket)
        if not peers:
            self._connections.pop(session_id, None)

    async def broadcast(self, session_id: int, message: AgentWsServerMessage) -> None:
        """
        向同一 session_id 下的全部连接推送 JSON 信封。

        Args:
            session_id: 目标会话
            message: 判别式服务端消息
        """
        peers = list(self._connections.get(session_id, ()))
        if not peers:
            return
        data = message.model_dump()
        stale: list[WebSocket] = []
        for websocket in peers:
            try:
                # 测试替身可能没有 client_state；缺省视为仍可发送
                state = getattr(websocket, "client_state", WebSocketState.CONNECTED)
                if state != WebSocketState.CONNECTED:
                    stale.append(websocket)
                    continue
                await websocket.send_json(data)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            await self.disconnect(session_id, websocket)


class WsHitlEventPublisher:
    """
    HitlEventPublisher 实现：把 hitl_* 事件映射为 AgentWsServerMessage 并 broadcast。

    仅推送安全摘要字段，过滤 message/command/password 等原始动作载荷键。
    """

    def __init__(self, hub: AgentWsHub | None = None) -> None:
        """
        绑定广播 Hub；默认使用模块单例。

        Args:
            hub: 可选注入的 Hub，便于单测隔离；省略时使用模块级 hub
        """
        # 参数名 hub 会遮蔽模块全局，故用 _bound_hub 保存显式注入值
        self._bound_hub = hub

    def _resolve_hub(self) -> AgentWsHub:
        """返回注入的 Hub，未注入时回落到模块单例。"""
        if self._bound_hub is not None:
            return self._bound_hub
        return hub

    async def publish(
        self,
        *,
        session_id: int,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        """
        发布不含原始动作载荷的 HITL 安全事件。

        Args:
            session_id: 目标会话
            event_type: HITL 事件名（hitl_pending / hitl_resolved / hitl_execution_failed）
            payload: 上游摘要；多余键会被丢弃
        """
        if event_type not in _HITL_EVENT_TYPES:
            return
        safe_payload: dict[str, Any] = {
            key: payload[key] for key in _HITL_SAFE_KEYS if key in payload
        }
        # event_type 已通过 _HITL_EVENT_TYPES 收窄，与 AgentWsEventType 子集一致
        message = AgentWsServerMessage(
            type=cast(AgentWsEventType, event_type),
            payload=safe_payload,
        )
        await self._resolve_hub().broadcast(session_id, message)


# 模块单例，供 API / chat_turn / HITL 注入
hub = AgentWsHub()
