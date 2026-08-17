"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl.py
@DateTime: 2026-08-12 11:36
@Docs: HITL 审批 API 的请求与响应模型（审批人可见完整 action_payload）。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.schemas.common import ApiModel

# 动态凭据是一次性明文口令，用 SecretStr 而不是 str：任何 repr()、model_dump()、
# 异常链或未来新增的日志都不会带出明文。main.py 的 422 处理器已经剥掉了校验
# 错误里的 input 字段，这里是纵深防御的第二层。
type DynamicCredentialPassword = SecretStr | None


class HitlDecideRequest(BaseModel):
    """人工审批请求体。"""

    model_config = ConfigDict(extra="forbid")

    approve: bool
    dynamic_credential_password: DynamicCredentialPassword = Field(
        default=None, min_length=1, max_length=256
    )


class HitlRetryRequest(BaseModel):
    """重试执行已批准提案的请求体。"""

    model_config = ConfigDict(extra="forbid")

    dynamic_credential_password: DynamicCredentialPassword = Field(
        default=None, min_length=1, max_length=256
    )


class HitlUnknownResolutionRequest(BaseModel):
    """人工处置 UNKNOWN 结果不确定提案的请求体。"""

    model_config = ConfigDict(extra="forbid")

    resolution: Literal["confirm_executed", "allow_retry"]


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
    execution_started_at: datetime | None = None
    status_reason: str | None = None
    resolved_by_user_id: int | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    result_excerpt: str | None = None
    asset_credential_type: str | None = None
    # 审批成功但执行未启动时的原因。非 None 表示提案已 APPROVED、可直接重试，
    # 前端据此把主操作从「批准」切换成「重试执行」而不是显示「批准失败」。
    execution_error: str | None = None
