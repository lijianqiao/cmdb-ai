"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: agent_session.py
@DateTime: 2026-08-13
@Docs: Agent 会话 REST API 的请求与响应模型。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.schemas.common import ApiModel

type ApprovalMode = Literal["ask", "assist", "full"]


class AgentSessionCreate(ApiModel):
    """创建会话请求体；title 可选，缺省为空字符串。"""

    title: str = Field(default="", max_length=200)


class AgentSessionApprovalUpdate(ApiModel):
    """更新会话审批模式请求体。"""

    approval_mode: ApprovalMode


class AgentSessionResponse(ApiModel):
    """会话详情/列表项。"""

    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)

    id: int
    user_id: int
    title: str
    status: str
    approval_mode: ApprovalMode
    created_at: datetime
    updated_at: datetime


class AgentMessageCreate(ApiModel):
    """发送用户消息并触发一轮 Agent turn。"""

    content: str = Field(min_length=1, max_length=16_000)


class AgentChatTurnResponse(ApiModel):
    """一轮对话结束后的 HTTP 摘要（实时细节走 WebSocket）。"""

    reason: str
    final_answer: str | None = None
    control: str | None = None


class AgentMessageResponse(ApiModel):
    """根 transcript 中的一条消息（已落库字段）。"""

    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)

    id: int
    session_id: int
    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    created_at: datetime
