"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl.py
@DateTime: 2026-08-12 11:36
@Docs: HITL 审批 API 的请求与响应模型（审批人可见完整 action_payload）。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ApiModel


class HitlDecideRequest(BaseModel):
    """人工审批请求体。"""

    model_config = ConfigDict(extra="forbid")

    approve: bool
    dynamic_credential_password: str | None = Field(default=None, min_length=1, max_length=256)


class HitlRetryRequest(BaseModel):
    """重试执行已批准提案的请求体。"""

    model_config = ConfigDict(extra="forbid")

    dynamic_credential_password: str | None = Field(default=None, min_length=1, max_length=256)


class HitlProposalResponse(ApiModel):
    """审批人视角的提案详情，包含完整动作载荷。"""

    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)

    id: int
    session_id: int
    proposed_by_agent_id: str | None
    action_type: str
    action_payload: dict[str, object]
    status: str
    reviewed_by_user_id: int | None
    reviewed_at: datetime | None
    executed_at: datetime | None
    created_at: datetime
    result_excerpt: str | None = None
    asset_credential_type: str | None = None
