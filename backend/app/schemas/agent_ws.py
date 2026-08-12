"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: agent_ws.py
@DateTime: 2026-08-12 12:30
@Docs: Agent WebSocket 判别式消息契约（服务端推送与客户端鉴权首帧）。

实现流程：
1. 用 Literal 枚举服务端事件类型，前端与后端共用同一组字符串，避免自由文本漂移。
2. AgentWsServerMessage 统一为 type + payload 信封：payload 保持 dict，由各事件自行约定字段。
3. AgentWsClientAuth 仅描述可选的首帧鉴权消息，供后续 WS 路由解析（本任务不挂路由）。
4. HITL 相关事件的 payload 应只含安全摘要字段，原始动作载荷不得进入此信封。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AgentWsEventType = Literal[
    "assistant_delta",
    "tool_call",
    "hitl_pending",
    "hitl_resolved",
    "hitl_execution_failed",
    "monitor_alert",
    "error",
    "turn_done",
]


class AgentWsServerMessage(BaseModel):
    """服务端推送给客户端的判别式 WebSocket 消息。"""

    model_config = ConfigDict(extra="forbid")

    type: AgentWsEventType
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentWsClientAuth(BaseModel):
    """客户端可选首帧鉴权消息（query token 之外的兼容路径）。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["auth"]
    access_token: str
