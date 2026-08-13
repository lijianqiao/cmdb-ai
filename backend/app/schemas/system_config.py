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


class LlmSystemConfigUpdate(ApiModel):
    """更新 LLM 与 Embedding 运行配置。"""

    chat_base_url: str = Field(min_length=1, max_length=2048)
    chat_api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_chat_api_key: bool = False
    chat_model: str = Field(min_length=1, max_length=200)
    chat_input_cost_per_million_usd: float = Field(ge=0, allow_inf_nan=False)
    chat_output_cost_per_million_usd: float = Field(ge=0, allow_inf_nan=False)
    embedding_base_url: str = Field(min_length=1, max_length=2048)
    embedding_api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_embedding_api_key: bool = False
    embedding_model: str = Field(min_length=1, max_length=200)

    @field_validator("chat_base_url", "embedding_base_url", mode="before")
    @classmethod
    def validate_base_url(cls, value: object) -> str:
        """校验并规范化 Base URL。"""
        return normalize_base_url(str(value))

    @model_validator(mode="after")
    def reject_conflicting_api_key_updates(self) -> Self:
        """禁止同一次请求同时提交新 Key 与清空标志。"""
        if self.clear_chat_api_key and self.chat_api_key is not None:
            raise ValueError("不能同时提交新的 chat_api_key 与 clear_chat_api_key=true")
        if self.clear_embedding_api_key and self.embedding_api_key is not None:
            raise ValueError(
                "不能同时提交新的 embedding_api_key 与 clear_embedding_api_key=true"
            )
        return self


class OperationsSystemConfigUpdate(ApiModel):
    """更新 HITL 与监控运行参数。"""

    hitl_notify_auto_approve: bool
    monitor_probe_timeout_seconds: float = Field(gt=0, le=30, allow_inf_nan=False)
    monitor_sweep_interval_seconds: float = Field(ge=5, le=3600, allow_inf_nan=False)
    cmdb_diff_interval_seconds: float = Field(ge=60, le=86_400, allow_inf_nan=False)
    monitor_event_retention_days: int = Field(ge=1, le=90)


class LlmSystemConfigResponse(ApiModel):
    """LLM 有效配置响应，不包含 API Key 明文或密文。"""

    chat_base_url: str
    chat_model: str
    chat_input_cost_per_million_usd: float
    chat_output_cost_per_million_usd: float
    chat_api_key_configured: bool
    chat_api_key_source: ConfigValueSource
    embedding_base_url: str
    embedding_model: str
    embedding_api_key_configured: bool
    embedding_api_key_source: ConfigValueSource


class OperationsSystemConfigResponse(ApiModel):
    """运行参数有效配置响应。"""

    hitl_notify_auto_approve: bool
    monitor_probe_timeout_seconds: float
    monitor_sweep_interval_seconds: float
    cmdb_diff_interval_seconds: float
    monitor_event_retention_days: int


class SystemConfigResponse(ApiModel):
    """系统配置完整响应。"""

    llm: LlmSystemConfigResponse
    operations: OperationsSystemConfigResponse
