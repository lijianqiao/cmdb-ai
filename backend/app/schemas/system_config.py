"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: system_config.py
@DateTime: 2026-08-13 12:55
@Docs: 系统运行配置请求、响应模型与 URL 校验。
"""

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from app.schemas.common import ApiModel

type ConfigValueSource = Literal["database", "environment", "unset"]


def normalize_base_url(value: str) -> str:
    """
    规范化并校验 LLM Base URL。

    Args:
        value: 原始 URL 字符串

    Returns:
        去除首尾空白与末尾斜杠后的合法 URL

    Raises:
        ValueError: URL 不符合 http/https 约束时
    """
    trimmed = value.strip()
    if trimmed.endswith("/"):
        trimmed = trimmed.rstrip("/")
    parts = urlsplit(trimmed)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Base URL 仅支持 http 或 https 协议")
    if not parts.netloc:
        raise ValueError("Base URL 必须包含主机名")
    if parts.username or parts.password:
        raise ValueError("Base URL 不能包含用户名或密码")
    if parts.query or parts.fragment:
        raise ValueError("Base URL 不能包含查询参数或片段")
    if not parts.hostname:
        raise ValueError("Base URL 必须包含主机名")
    return trimmed


type ChatTier = Literal["fast", "balanced", "strong"]

CHAT_TIERS: tuple[ChatTier, ...] = ("fast", "balanced", "strong")


class ChatTierUpdate(ApiModel):
    """单个 chat 档位的配置更新。

    便宜档与强档允许 base_url / model 留空——那表示"这一档没配"，
    运行时整档回退到平衡档。平衡档不允许留空，由 LlmSystemConfigUpdate 统一校验：
    它是回退目标，空了就没有任何一档可用。
    """

    base_url: str = Field(default="", max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    model: str = Field(default="", max_length=200)
    input_cost_per_million_usd: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    output_cost_per_million_usd: float = Field(default=0.0, ge=0, allow_inf_nan=False)

    @field_validator("base_url", mode="before")
    @classmethod
    def validate_base_url(cls, value: object) -> str:
        """校验并规范化 Base URL；空串直接放行，代表这一档未配置。"""
        text = str(value).strip()
        return normalize_base_url(text) if text else ""

    @model_validator(mode="after")
    def reject_conflicting_api_key_update(self) -> Self:
        """禁止同一次请求同时提交新 Key 与清空标志。"""
        if self.clear_api_key and self.api_key is not None:
            raise ValueError("不能同时提交新的 api_key 与 clear_api_key=true")
        return self


class LlmSystemConfigUpdate(ApiModel):
    """更新 LLM 三档 chat 与 Embedding 运行配置。"""

    chat_fast: ChatTierUpdate = Field(default_factory=ChatTierUpdate)
    chat_balanced: ChatTierUpdate
    chat_strong: ChatTierUpdate = Field(default_factory=ChatTierUpdate)
    embedding_base_url: str = Field(min_length=1, max_length=2048)
    embedding_api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_embedding_api_key: bool = False
    embedding_model: str = Field(min_length=1, max_length=200)

    @field_validator("embedding_base_url", mode="before")
    @classmethod
    def validate_base_url(cls, value: object) -> str:
        """校验并规范化 Base URL。"""
        return normalize_base_url(str(value))

    @model_validator(mode="after")
    def require_balanced_tier_and_reject_conflicts(self) -> Self:
        """平衡档必填；禁止同时提交新 Key 与清空标志。"""
        if not self.chat_balanced.base_url or not self.chat_balanced.model:
            raise ValueError("平衡档的 Base URL 与模型名必填，它是其它档位的回退目标")
        if self.clear_embedding_api_key and self.embedding_api_key is not None:
            raise ValueError(
                "不能同时提交新的 embedding_api_key 与 clear_embedding_api_key=true"
            )
        return self


class OperationsSystemConfigUpdate(ApiModel):
    """更新监控与 CMDB 巡检运行参数。"""

    monitor_probe_timeout_seconds: float = Field(gt=0, le=30, allow_inf_nan=False)
    monitor_sweep_interval_seconds: float = Field(ge=5, le=3600, allow_inf_nan=False)
    cmdb_diff_interval_seconds: float = Field(ge=60, le=86_400, allow_inf_nan=False)
    monitor_event_retention_days: int = Field(ge=1, le=90)


class ChatTierResponse(ApiModel):
    """单个 chat 档位的有效配置，不包含 API Key 明文或密文。

    `configured=False` 时下面的连接信息与单价全部是平衡档的值——
    管理页要据此显示「未配置，当前回退到平衡档」，否则用户会以为便宜档在生效、
    实际上钱按平衡档在花。
    """

    base_url: str
    model: str
    input_cost_per_million_usd: float
    output_cost_per_million_usd: float
    api_key_configured: bool
    api_key_source: ConfigValueSource
    configured: bool
    effective_tier: ChatTier


class LlmSystemConfigResponse(ApiModel):
    """LLM 有效配置响应，不包含 API Key 明文或密文。"""

    chat_fast: ChatTierResponse
    chat_balanced: ChatTierResponse
    chat_strong: ChatTierResponse
    embedding_base_url: str
    embedding_model: str
    embedding_api_key_configured: bool
    embedding_api_key_source: ConfigValueSource


class OperationsSystemConfigResponse(ApiModel):
    """运行参数有效配置响应。"""

    monitor_probe_timeout_seconds: float
    monitor_sweep_interval_seconds: float
    cmdb_diff_interval_seconds: float
    monitor_event_retention_days: int


class SystemConfigResponse(ApiModel):
    """系统配置完整响应。"""

    llm: LlmSystemConfigResponse
    operations: OperationsSystemConfigResponse
