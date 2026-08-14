"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: ws_hub.py
@DateTime: 2026-08-12 12:30
@Docs: 进程内 Agent WebSocket 连接 Hub，以及将 HITL 事件映射为安全 WS 推送的发布器。

实现流程：
1. AgentWsHub 用 session_id → WebSocket 集合管理连接；broadcast 只发给同一会话的连接。
2. 每个连接有独立有界发送队列与 writer 任务；broadcast 只做 put_nowait，不串行等待网络。
3. 队列满或单次发送超时时只清理慢连接，不影响同会话其他 peer。
4. WsHitlEventPublisher 实现 T10 HitlEventPublisher Protocol：只接受 hitl_* 事件类型，
   并把 payload 过滤到 ProposalSafeSummary 白名单后再 broadcast。
5. 模块级 hub 单例供后续 API / chat_turn / HITL 注入，本任务不挂 HTTP/WS 路由。
"""

import asyncio
from collections import defaultdict
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from starlette.websockets import WebSocket, WebSocketState

from app.agent.spawn import ChildReceipt
from app.schemas.agent_ws import AgentWsEventType, AgentWsServerMessage

# 与 T10 ProposalSafeSummary 字段对齐；绝不透传原始动作载荷
_HITL_SAFE_KEYS = frozenset(
    {
        "proposal_id",
        "action_type",
        "status",
        "status_reason",
        "reason",
        "asset_id",
        "result_excerpt",
        "last_error",
        "execution_started_at",
        "resolved_at",
    }
)
_HITL_EVENT_TYPES = frozenset({"hitl_pending", "hitl_resolved", "hitl_execution_failed"})
_CHILD_SAFE_KEYS = frozenset(
    {
        "child_id",
        "role",
        "task_brief",
        "status",
        "result_summary",
        "created_at",
        "status_changed_at",
    }
)


@dataclass(slots=True)
class _Peer:
    """单个 WebSocket 连接的发送队列与 writer 任务。"""

    queue: asyncio.Queue[AgentWsServerMessage]
    writer_task: asyncio.Task[None]


def _child_safe_payload(receipt: ChildReceipt) -> dict[str, Any]:
    """从 ChildReceipt 提取 WS 白名单字段。"""

    def _iso(value: datetime) -> str:
        return value.isoformat()

    return {
        "child_id": receipt.child_id,
        "role": receipt.role,
        "task_brief": receipt.task_brief,
        "status": receipt.status,
        "result_summary": receipt.result_summary,
        "created_at": _iso(receipt.created_at),
        "status_changed_at": _iso(receipt.status_changed_at),
    }


class AgentWsHub:
    """进程内按会话隔离的 WebSocket 连接表与广播器。"""

    def __init__(
        self,
        queue_size: int = 128,
        send_timeout_seconds: float = 5.0,
    ) -> None:
        """
        初始化空连接表与发送参数。

        Args:
            queue_size: 每连接有界队列容量，默认 128
            send_timeout_seconds: 单次 send_json 超时秒数，默认 5
        """
        self._queue_size = queue_size
        self._send_timeout_seconds = send_timeout_seconds
        self._connections: dict[int, dict[WebSocket, _Peer]] = defaultdict(dict)

    async def connect(self, session_id: int, websocket: WebSocket) -> None:
        """
        将 WebSocket 注册到指定会话并启动 writer 任务。

        Args:
            session_id: Agent 会话主键
            websocket: 已接受的 WebSocket 连接
        """
        queue: asyncio.Queue[AgentWsServerMessage] = asyncio.Queue(maxsize=self._queue_size)
        writer_task = asyncio.create_task(self._writer(session_id, websocket, queue))
        self._connections[session_id][websocket] = _Peer(queue=queue, writer_task=writer_task)

    async def disconnect(
        self,
        session_id: int,
        websocket: WebSocket,
        *,
        from_writer: bool = False,
    ) -> None:
        """
        从指定会话注销 WebSocket 并结束 writer 任务。

        Args:
            session_id: Agent 会话主键
            websocket: 要移除的连接
            from_writer: writer 任务自行触发清理时为 True，避免 cancel/await 自身
        """
        peers = self._connections.get(session_id)
        if peers is None:
            return
        peer = peers.pop(websocket, None)
        if not peers:
            self._connections.pop(session_id, None)
        if peer is None:
            return
        if not from_writer:
            peer.writer_task.cancel()
            with suppress(asyncio.CancelledError):
                await peer.writer_task
        with suppress(Exception):
            await websocket.close()

    async def broadcast(self, session_id: int, message: AgentWsServerMessage) -> None:
        """
        向同一 session_id 下的全部连接入队 JSON 信封，不等待网络发送完成。

        Args:
            session_id: 目标会话
            message: 判别式服务端消息
        """
        peers = self._connections.get(session_id)
        if not peers:
            return
        for websocket, peer in list(peers.items()):
            try:
                state = getattr(websocket, "client_state", WebSocketState.CONNECTED)
                if state != WebSocketState.CONNECTED:
                    asyncio.create_task(self.disconnect(session_id, websocket))
                    continue
                peer.queue.put_nowait(message)
            except asyncio.QueueFull:
                asyncio.create_task(self.disconnect(session_id, websocket))
            except Exception:
                asyncio.create_task(self.disconnect(session_id, websocket))

    async def _writer(
        self,
        session_id: int,
        websocket: WebSocket,
        queue: asyncio.Queue[AgentWsServerMessage],
    ) -> None:
        """
        从队列取出消息并发送到 WebSocket；失败时清理本连接。

        Args:
            session_id: 所属会话
            websocket: 目标连接
            queue: 有界发送队列
        """
        try:
            while True:
                message = await queue.get()
                await asyncio.wait_for(
                    websocket.send_json(message.model_dump(mode="json")),
                    timeout=self._send_timeout_seconds,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.disconnect(session_id, websocket, from_writer=True)


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
        safe_payload: dict[str, Any] = {key: payload[key] for key in _HITL_SAFE_KEYS if key in payload}
        # event_type 已通过 _HITL_EVENT_TYPES 收窄，与 AgentWsEventType 子集一致
        message = AgentWsServerMessage(
            type=cast(AgentWsEventType, event_type),
            payload=safe_payload,
        )
        await self._resolve_hub().broadcast(session_id, message)


class WsSpawnEventPublisher:
    """
    SpawnEventPublisher 实现：把子 Agent 状态映射为 child_status WS 消息。

    仅推送安全摘要字段，过滤 budget、tools_allowlist、artifacts 等敏感键。
    """

    def __init__(self, hub: AgentWsHub | None = None) -> None:
        """
        绑定广播 Hub；默认使用模块单例。

        Args:
            hub: 可选注入的 Hub，便于单测隔离；省略时使用模块级 hub
        """
        self._bound_hub = hub

    def _resolve_hub(self) -> AgentWsHub:
        """返回注入的 Hub，未注入时回落到模块单例。"""
        if self._bound_hub is not None:
            return self._bound_hub
        return hub

    async def publish_child_status(self, receipt: ChildReceipt) -> None:
        """
        发布不含凭据与原始产物的子 Agent 安全状态。

        Args:
            receipt: 已持久化的子 Agent 回执
        """
        safe_payload = _child_safe_payload(receipt)
        message = AgentWsServerMessage(
            type="child_status",
            payload=safe_payload,
        )
        await self._resolve_hub().broadcast(receipt.session_id, message)


class BufferedWsHitlEventPublisher(WsHitlEventPublisher):
    """缓冲 hitl_* 事件，由调用方在 db.commit() 之后 flush。

    HITL 提案在一轮对话的事务内创建；如果事件在提交前就广播，前端收到
    事件后立即用另一个数据库会话发 GET /hitl/proposals/{id}，会因为行未提交
    而拿到 404「HITL 提案不存在」。缓冲到提交之后再发可消除这个竞态。
    """

    def __init__(self, hub: AgentWsHub | None = None) -> None:
        """绑定 Hub 并初始化空缓冲队列。"""
        super().__init__(hub)
        self._pending: list[tuple[int, str, dict[str, object]]] = []

    async def publish(
        self,
        *,
        session_id: int,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        """只入队，不立即广播。"""
        self._pending.append((session_id, event_type, dict(payload)))

    async def flush(self) -> None:
        """把缓冲的事件按顺序广播出去；重复调用是空操作。"""
        pending, self._pending = self._pending, []
        for session_id, event_type, payload in pending:
            await super().publish(
                session_id=session_id,
                event_type=event_type,
                payload=payload,
            )


# 模块单例，供 API / chat_turn / HITL 注入
hub = AgentWsHub()
