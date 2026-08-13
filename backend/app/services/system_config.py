"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: system_config.py
@DateTime: 2026-08-13 12:55
@Docs: 系统运行配置白名单、有效值解析与持久化服务。
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.data_encryption import decrypt_secret, encrypt_secret
from app.crud.system_config import system_config_crud
from app.models.system_config import SystemConfig
from app.schemas.system_config import (
    ConfigValueSource,
    LlmSystemConfigResponse,
    LlmSystemConfigUpdate,
    OperationsSystemConfigResponse,
    OperationsSystemConfigUpdate,
    SystemConfigResponse,
)

KEY_LLM_CHAT_BASE_URL = "LLM_CHAT_BASE_URL"
KEY_LLM_CHAT_API_KEY = "LLM_CHAT_API_KEY"
KEY_LLM_CHAT_MODEL = "LLM_CHAT_MODEL"
KEY_LLM_CHAT_INPUT_COST_PER_MILLION_USD = "LLM_CHAT_INPUT_COST_PER_MILLION_USD"
KEY_LLM_CHAT_OUTPUT_COST_PER_MILLION_USD = "LLM_CHAT_OUTPUT_COST_PER_MILLION_USD"
KEY_LLM_EMBEDDING_BASE_URL = "LLM_EMBEDDING_BASE_URL"
KEY_LLM_EMBEDDING_API_KEY = "LLM_EMBEDDING_API_KEY"
KEY_LLM_EMBEDDING_MODEL = "LLM_EMBEDDING_MODEL"
KEY_HITL_NOTIFY_AUTO_APPROVE = "HITL_NOTIFY_AUTO_APPROVE"
KEY_MONITOR_PROBE_TIMEOUT_SECONDS = "MONITOR_PROBE_TIMEOUT_SECONDS"
KEY_MONITOR_SWEEP_INTERVAL_SECONDS = "MONITOR_SWEEP_INTERVAL_SECONDS"
KEY_CMDB_DIFF_INTERVAL_SECONDS = "CMDB_DIFF_INTERVAL_SECONDS"
KEY_MONITOR_EVENT_RETENTION_DAYS = "MONITOR_EVENT_RETENTION_DAYS"

LLM_CONFIG_KEYS = (
    KEY_LLM_CHAT_BASE_URL,
    KEY_LLM_CHAT_API_KEY,
    KEY_LLM_CHAT_MODEL,
    KEY_LLM_CHAT_INPUT_COST_PER_MILLION_USD,
    KEY_LLM_CHAT_OUTPUT_COST_PER_MILLION_USD,
    KEY_LLM_EMBEDDING_BASE_URL,
    KEY_LLM_EMBEDDING_API_KEY,
    KEY_LLM_EMBEDDING_MODEL,
)

OPERATIONS_CONFIG_KEYS = (
    KEY_HITL_NOTIFY_AUTO_APPROVE,
    KEY_MONITOR_PROBE_TIMEOUT_SECONDS,
    KEY_MONITOR_SWEEP_INTERVAL_SECONDS,
    KEY_CMDB_DIFF_INTERVAL_SECONDS,
    KEY_MONITOR_EVENT_RETENTION_DAYS,
)

ALL_SYSTEM_CONFIG_KEYS = LLM_CONFIG_KEYS + OPERATIONS_CONFIG_KEYS


@dataclass(frozen=True, slots=True)
class EffectiveLlmConfig:
    """LLM 与 Embedding 有效运行配置快照。"""

    chat_base_url: str
    chat_api_key: str
    chat_api_key_source: ConfigValueSource
    chat_model: str
    chat_input_cost_per_million_usd: float
    chat_output_cost_per_million_usd: float
    embedding_base_url: str
    embedding_api_key: str
    embedding_api_key_source: ConfigValueSource
    embedding_model: str

    @property
    def chat_api_key_configured(self) -> bool:
        """Chat API Key 是否已配置。"""
        return bool(self.chat_api_key)

    @property
    def embedding_api_key_configured(self) -> bool:
        """Embedding API Key 是否已配置。"""
        return bool(self.embedding_api_key)


@dataclass(frozen=True, slots=True)
class EffectiveOperationsConfig:
    """HITL 与监控有效运行配置快照。"""

    hitl_notify_auto_approve: bool
    monitor_probe_timeout_seconds: float
    monitor_sweep_interval_seconds: float
    cmdb_diff_interval_seconds: float
    monitor_event_retention_days: int


def _resolve_string_value(
    row: SystemConfig | None,
    fallback: str,
) -> str:
    if row is None or row.value is None:
        return fallback
    return row.value


def _resolve_float_value(row: SystemConfig | None, fallback: float) -> float:
    if row is None or row.value is None:
        return fallback
    return float(row.value)


def _resolve_int_value(row: SystemConfig | None, fallback: int) -> int:
    if row is None or row.value is None:
        return fallback
    return int(row.value)


def _resolve_bool_value(row: SystemConfig | None, fallback: bool) -> bool:
    if row is None or row.value is None:
        return fallback
    return row.value.strip().lower() == "true"


def _resolve_api_key(
    row: SystemConfig | None,
    env_value: str,
) -> tuple[str, ConfigValueSource]:
    if row is not None:
        if row.value is None:
            return "", "database"
        return decrypt_secret(row.value), "database"
    if env_value:
        return env_value, "environment"
    return "", "unset"


async def get_effective_llm_config(db: AsyncSession) -> EffectiveLlmConfig:
    """
    读取并合并 LLM 八项有效配置。

    Args:
        db: 异步数据库会话

    Returns:
        含解密后 API Key 的有效配置快照
    """
    rows = await system_config_crud.get_by_keys(db, LLM_CONFIG_KEYS)
    chat_api_key, chat_api_key_source = _resolve_api_key(
        rows.get(KEY_LLM_CHAT_API_KEY),
        settings.llm_chat_api_key,
    )
    embedding_api_key, embedding_api_key_source = _resolve_api_key(
        rows.get(KEY_LLM_EMBEDDING_API_KEY),
        settings.llm_embedding_api_key,
    )
    validated = LlmSystemConfigUpdate(
        chat_base_url=_resolve_string_value(
            rows.get(KEY_LLM_CHAT_BASE_URL),
            settings.LLM_CHAT_BASE_URL,
        ),
        chat_model=_resolve_string_value(
            rows.get(KEY_LLM_CHAT_MODEL),
            settings.LLM_CHAT_MODEL,
        ),
        chat_input_cost_per_million_usd=_resolve_float_value(
            rows.get(KEY_LLM_CHAT_INPUT_COST_PER_MILLION_USD),
            settings.LLM_CHAT_INPUT_COST_PER_MILLION_USD,
        ),
        chat_output_cost_per_million_usd=_resolve_float_value(
            rows.get(KEY_LLM_CHAT_OUTPUT_COST_PER_MILLION_USD),
            settings.LLM_CHAT_OUTPUT_COST_PER_MILLION_USD,
        ),
        embedding_base_url=_resolve_string_value(
            rows.get(KEY_LLM_EMBEDDING_BASE_URL),
            settings.LLM_EMBEDDING_BASE_URL,
        ),
        embedding_model=_resolve_string_value(
            rows.get(KEY_LLM_EMBEDDING_MODEL),
            settings.LLM_EMBEDDING_MODEL,
        ),
    )
    return EffectiveLlmConfig(
        chat_base_url=validated.chat_base_url,
        chat_api_key=chat_api_key,
        chat_api_key_source=chat_api_key_source,
        chat_model=validated.chat_model,
        chat_input_cost_per_million_usd=validated.chat_input_cost_per_million_usd,
        chat_output_cost_per_million_usd=validated.chat_output_cost_per_million_usd,
        embedding_base_url=validated.embedding_base_url,
        embedding_api_key=embedding_api_key,
        embedding_api_key_source=embedding_api_key_source,
        embedding_model=validated.embedding_model,
    )


async def get_effective_operations_config(db: AsyncSession) -> EffectiveOperationsConfig:
    """
    读取并校验 HITL 与监控五项有效配置。

    Args:
        db: 异步数据库会话

    Returns:
        通过范围校验的有效运行参数快照
    """
    rows = await system_config_crud.get_by_keys(db, OPERATIONS_CONFIG_KEYS)
    validated = OperationsSystemConfigUpdate(
        hitl_notify_auto_approve=_resolve_bool_value(
            rows.get(KEY_HITL_NOTIFY_AUTO_APPROVE),
            settings.HITL_NOTIFY_AUTO_APPROVE,
        ),
        monitor_probe_timeout_seconds=_resolve_float_value(
            rows.get(KEY_MONITOR_PROBE_TIMEOUT_SECONDS),
            settings.MONITOR_PROBE_TIMEOUT_SECONDS,
        ),
        monitor_sweep_interval_seconds=_resolve_float_value(
            rows.get(KEY_MONITOR_SWEEP_INTERVAL_SECONDS),
            settings.MONITOR_SWEEP_INTERVAL_SECONDS,
        ),
        cmdb_diff_interval_seconds=_resolve_float_value(
            rows.get(KEY_CMDB_DIFF_INTERVAL_SECONDS),
            settings.CMDB_DIFF_INTERVAL_SECONDS,
        ),
        monitor_event_retention_days=_resolve_int_value(
            rows.get(KEY_MONITOR_EVENT_RETENTION_DAYS),
            settings.MONITOR_EVENT_RETENTION_DAYS,
        ),
    )
    return EffectiveOperationsConfig(
        hitl_notify_auto_approve=validated.hitl_notify_auto_approve,
        monitor_probe_timeout_seconds=validated.monitor_probe_timeout_seconds,
        monitor_sweep_interval_seconds=validated.monitor_sweep_interval_seconds,
        cmdb_diff_interval_seconds=validated.cmdb_diff_interval_seconds,
        monitor_event_retention_days=validated.monitor_event_retention_days,
    )


async def save_llm_config(
    db: AsyncSession,
    payload: LlmSystemConfigUpdate,
    *,
    updated_by_user_id: int | None,
) -> None:
    """
    持久化 LLM 配置更新。

    Args:
        db: 异步数据库会话
        payload: 已校验的更新载荷
        updated_by_user_id: 更新人用户 ID
    """
    values: dict[str, str | None] = {
        KEY_LLM_CHAT_BASE_URL: payload.chat_base_url,
        KEY_LLM_CHAT_MODEL: payload.chat_model,
        KEY_LLM_CHAT_INPUT_COST_PER_MILLION_USD: str(
            payload.chat_input_cost_per_million_usd
        ),
        KEY_LLM_CHAT_OUTPUT_COST_PER_MILLION_USD: str(
            payload.chat_output_cost_per_million_usd
        ),
        KEY_LLM_EMBEDDING_BASE_URL: payload.embedding_base_url,
        KEY_LLM_EMBEDDING_MODEL: payload.embedding_model,
    }
    if payload.clear_chat_api_key:
        values[KEY_LLM_CHAT_API_KEY] = None
    elif payload.chat_api_key is not None:
        values[KEY_LLM_CHAT_API_KEY] = encrypt_secret(
            payload.chat_api_key.get_secret_value()
        )
    if payload.clear_embedding_api_key:
        values[KEY_LLM_EMBEDDING_API_KEY] = None
    elif payload.embedding_api_key is not None:
        values[KEY_LLM_EMBEDDING_API_KEY] = encrypt_secret(
            payload.embedding_api_key.get_secret_value()
        )
    await system_config_crud.upsert_values(
        db,
        values,
        updated_by_user_id=updated_by_user_id,
    )


async def save_operations_config(
    db: AsyncSession,
    payload: OperationsSystemConfigUpdate,
    *,
    updated_by_user_id: int | None,
) -> None:
    """
    持久化运行参数更新。

    Args:
        db: 异步数据库会话
        payload: 已校验的更新载荷
        updated_by_user_id: 更新人用户 ID
    """
    await system_config_crud.upsert_values(
        db,
        {
            KEY_HITL_NOTIFY_AUTO_APPROVE: (
                "true" if payload.hitl_notify_auto_approve else "false"
            ),
            KEY_MONITOR_PROBE_TIMEOUT_SECONDS: str(
                payload.monitor_probe_timeout_seconds
            ),
            KEY_MONITOR_SWEEP_INTERVAL_SECONDS: str(
                payload.monitor_sweep_interval_seconds
            ),
            KEY_CMDB_DIFF_INTERVAL_SECONDS: str(payload.cmdb_diff_interval_seconds),
            KEY_MONITOR_EVENT_RETENTION_DAYS: str(
                payload.monitor_event_retention_days
            ),
        },
        updated_by_user_id=updated_by_user_id,
    )


async def build_system_config_response(db: AsyncSession) -> SystemConfigResponse:
    """
    构造脱敏后的系统配置响应。

    Args:
        db: 异步数据库会话

    Returns:
        不含 API Key 明文或密文的响应模型
    """
    llm = await get_effective_llm_config(db)
    operations = await get_effective_operations_config(db)
    return SystemConfigResponse(
        llm=LlmSystemConfigResponse(
            chat_base_url=llm.chat_base_url,
            chat_model=llm.chat_model,
            chat_input_cost_per_million_usd=llm.chat_input_cost_per_million_usd,
            chat_output_cost_per_million_usd=llm.chat_output_cost_per_million_usd,
            chat_api_key_configured=llm.chat_api_key_configured,
            chat_api_key_source=llm.chat_api_key_source,
            embedding_base_url=llm.embedding_base_url,
            embedding_model=llm.embedding_model,
            embedding_api_key_configured=llm.embedding_api_key_configured,
            embedding_api_key_source=llm.embedding_api_key_source,
        ),
        operations=OperationsSystemConfigResponse(
            hitl_notify_auto_approve=operations.hitl_notify_auto_approve,
            monitor_probe_timeout_seconds=operations.monitor_probe_timeout_seconds,
            monitor_sweep_interval_seconds=operations.monitor_sweep_interval_seconds,
            cmdb_diff_interval_seconds=operations.cmdb_diff_interval_seconds,
            monitor_event_retention_days=operations.monitor_event_retention_days,
        ),
    )
