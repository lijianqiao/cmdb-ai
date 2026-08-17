"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: system_config.py
@DateTime: 2026-08-13 12:55
@Docs: 系统运行配置白名单、有效值解析与持久化服务。
"""

from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.data_encryption import decrypt_secret, encrypt_secret
from app.crud.system_config import system_config_crud
from app.models.system_config import SystemConfig
from app.schemas.system_config import (
    CHAT_TIERS,
    ChatTier,
    ChatTierResponse,
    ChatTierUpdate,
    ConfigValueSource,
    LlmSystemConfigResponse,
    LlmSystemConfigUpdate,
    OperationsSystemConfigResponse,
    OperationsSystemConfigUpdate,
    SystemConfigResponse,
    normalize_base_url,
)

# 平衡档沿用不带档位后缀的键：它是既有配置（已经配好在跑），改名只会把
# 已落库的行弄丢；而且它是其它两档的回退目标，不带后缀反而更好读。
KEY_LLM_CHAT_BASE_URL = "LLM_CHAT_BASE_URL"
KEY_LLM_CHAT_API_KEY = "LLM_CHAT_API_KEY"
KEY_LLM_CHAT_MODEL = "LLM_CHAT_MODEL"
KEY_LLM_CHAT_INPUT_COST_PER_MILLION_USD = "LLM_CHAT_INPUT_COST_PER_MILLION_USD"
KEY_LLM_CHAT_OUTPUT_COST_PER_MILLION_USD = "LLM_CHAT_OUTPUT_COST_PER_MILLION_USD"
KEY_LLM_CHAT_FAST_BASE_URL = "LLM_CHAT_FAST_BASE_URL"
KEY_LLM_CHAT_FAST_API_KEY = "LLM_CHAT_FAST_API_KEY"
KEY_LLM_CHAT_FAST_MODEL = "LLM_CHAT_FAST_MODEL"
KEY_LLM_CHAT_FAST_INPUT_COST_PER_MILLION_USD = "LLM_CHAT_FAST_INPUT_COST_PER_MILLION_USD"
KEY_LLM_CHAT_FAST_OUTPUT_COST_PER_MILLION_USD = (
    "LLM_CHAT_FAST_OUTPUT_COST_PER_MILLION_USD"
)
KEY_LLM_CHAT_STRONG_BASE_URL = "LLM_CHAT_STRONG_BASE_URL"
KEY_LLM_CHAT_STRONG_API_KEY = "LLM_CHAT_STRONG_API_KEY"
KEY_LLM_CHAT_STRONG_MODEL = "LLM_CHAT_STRONG_MODEL"
KEY_LLM_CHAT_STRONG_INPUT_COST_PER_MILLION_USD = (
    "LLM_CHAT_STRONG_INPUT_COST_PER_MILLION_USD"
)
KEY_LLM_CHAT_STRONG_OUTPUT_COST_PER_MILLION_USD = (
    "LLM_CHAT_STRONG_OUTPUT_COST_PER_MILLION_USD"
)
KEY_LLM_EMBEDDING_BASE_URL = "LLM_EMBEDDING_BASE_URL"
KEY_LLM_EMBEDDING_API_KEY = "LLM_EMBEDDING_API_KEY"
KEY_LLM_EMBEDDING_MODEL = "LLM_EMBEDDING_MODEL"
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
    KEY_LLM_CHAT_FAST_BASE_URL,
    KEY_LLM_CHAT_FAST_API_KEY,
    KEY_LLM_CHAT_FAST_MODEL,
    KEY_LLM_CHAT_FAST_INPUT_COST_PER_MILLION_USD,
    KEY_LLM_CHAT_FAST_OUTPUT_COST_PER_MILLION_USD,
    KEY_LLM_CHAT_STRONG_BASE_URL,
    KEY_LLM_CHAT_STRONG_API_KEY,
    KEY_LLM_CHAT_STRONG_MODEL,
    KEY_LLM_CHAT_STRONG_INPUT_COST_PER_MILLION_USD,
    KEY_LLM_CHAT_STRONG_OUTPUT_COST_PER_MILLION_USD,
    KEY_LLM_EMBEDDING_BASE_URL,
    KEY_LLM_EMBEDDING_API_KEY,
    KEY_LLM_EMBEDDING_MODEL,
)

OPERATIONS_CONFIG_KEYS = (
    KEY_MONITOR_PROBE_TIMEOUT_SECONDS,
    KEY_MONITOR_SWEEP_INTERVAL_SECONDS,
    KEY_CMDB_DIFF_INTERVAL_SECONDS,
    KEY_MONITOR_EVENT_RETENTION_DAYS,
)

ALL_SYSTEM_CONFIG_KEYS = LLM_CONFIG_KEYS + OPERATIONS_CONFIG_KEYS


@dataclass(frozen=True, slots=True)
class EffectiveChatTier:
    """单个 chat 档位的有效运行配置。

    `configured=False` 时下面所有字段（含两个单价）都已经被替换成平衡档的值，
    `effective_tier` 记录实际生效的是哪一档。

    **单价必须一起回退**：只换连接信息、保留本档单价，会按便宜档的价格
    给平衡档的调用记账，界面上的花费数字就是编的。
    """

    base_url: str
    api_key: str
    api_key_source: ConfigValueSource
    model: str
    input_cost_per_million_usd: float
    output_cost_per_million_usd: float
    configured: bool
    effective_tier: ChatTier

    @property
    def api_key_configured(self) -> bool:
        """本档 API Key 是否已配置。"""
        return bool(self.api_key)


@dataclass(frozen=True, slots=True)
class EffectiveLlmConfig:
    """LLM 三档 chat 与 Embedding 有效运行配置快照。"""

    chat_fast: EffectiveChatTier
    chat_balanced: EffectiveChatTier
    chat_strong: EffectiveChatTier
    embedding_base_url: str
    embedding_api_key: str
    embedding_api_key_source: ConfigValueSource
    embedding_model: str

    def chat_tier(self, tier: ChatTier) -> EffectiveChatTier:
        """按档位名取有效配置。"""
        if tier == "fast":
            return self.chat_fast
        if tier == "strong":
            return self.chat_strong
        return self.chat_balanced

    @property
    def embedding_api_key_configured(self) -> bool:
        """Embedding API Key 是否已配置。"""
        return bool(self.embedding_api_key)


@dataclass(frozen=True, slots=True)
class EffectiveOperationsConfig:
    """监控与 CMDB 巡检有效运行配置快照。"""

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


@dataclass(frozen=True, slots=True)
class _TierKeys:
    """一个档位在配置表 / settings 里对应的键名与兜底默认值。"""

    base_url: str
    api_key: str
    model: str
    input_cost: str
    output_cost: str
    default_base_url: str
    default_api_key: str
    default_model: str
    default_input_cost: float
    default_output_cost: float


def _tier_keys(tier: ChatTier) -> _TierKeys:
    """返回某一档的配置键名与 .env 兜底默认值。"""
    if tier == "fast":
        return _TierKeys(
            base_url=KEY_LLM_CHAT_FAST_BASE_URL,
            api_key=KEY_LLM_CHAT_FAST_API_KEY,
            model=KEY_LLM_CHAT_FAST_MODEL,
            input_cost=KEY_LLM_CHAT_FAST_INPUT_COST_PER_MILLION_USD,
            output_cost=KEY_LLM_CHAT_FAST_OUTPUT_COST_PER_MILLION_USD,
            default_base_url=settings.LLM_CHAT_FAST_BASE_URL,
            default_api_key=settings.llm_chat_fast_api_key,
            default_model=settings.LLM_CHAT_FAST_MODEL,
            default_input_cost=settings.LLM_CHAT_FAST_INPUT_COST_PER_MILLION_USD,
            default_output_cost=settings.LLM_CHAT_FAST_OUTPUT_COST_PER_MILLION_USD,
        )
    if tier == "strong":
        return _TierKeys(
            base_url=KEY_LLM_CHAT_STRONG_BASE_URL,
            api_key=KEY_LLM_CHAT_STRONG_API_KEY,
            model=KEY_LLM_CHAT_STRONG_MODEL,
            input_cost=KEY_LLM_CHAT_STRONG_INPUT_COST_PER_MILLION_USD,
            output_cost=KEY_LLM_CHAT_STRONG_OUTPUT_COST_PER_MILLION_USD,
            default_base_url=settings.LLM_CHAT_STRONG_BASE_URL,
            default_api_key=settings.llm_chat_strong_api_key,
            default_model=settings.LLM_CHAT_STRONG_MODEL,
            default_input_cost=settings.LLM_CHAT_STRONG_INPUT_COST_PER_MILLION_USD,
            default_output_cost=settings.LLM_CHAT_STRONG_OUTPUT_COST_PER_MILLION_USD,
        )
    return _TierKeys(
        base_url=KEY_LLM_CHAT_BASE_URL,
        api_key=KEY_LLM_CHAT_API_KEY,
        model=KEY_LLM_CHAT_MODEL,
        input_cost=KEY_LLM_CHAT_INPUT_COST_PER_MILLION_USD,
        output_cost=KEY_LLM_CHAT_OUTPUT_COST_PER_MILLION_USD,
        default_base_url=settings.LLM_CHAT_BASE_URL,
        default_api_key=settings.llm_chat_api_key,
        default_model=settings.LLM_CHAT_MODEL,
        default_input_cost=settings.LLM_CHAT_INPUT_COST_PER_MILLION_USD,
        default_output_cost=settings.LLM_CHAT_OUTPUT_COST_PER_MILLION_USD,
    )


def _resolve_chat_tier(
    rows: dict[str, SystemConfig],
    tier: ChatTier,
    *,
    fallback: EffectiveChatTier | None,
) -> EffectiveChatTier:
    """解析一档 chat 配置；未配置且给了 fallback 时整份回退。

    **"已配置"= base_url 与 model 都非空。** API Key 不计入判定：
    本地部署的 vLLM / Ollama 合法地不需要 Key，把它算进来会让本地模型永远配不上。
    只填了一半（有 URL 没有模型名，或反过来）也算没配——那种状态发不出请求。

    Args:
        rows: 一次查出的配置行
        tier: 目标档位
        fallback: 平衡档的解析结果；平衡档自身传 None（它没有可回退的对象）

    Returns:
        该档的有效配置；回退时字段全部来自 fallback，仅 effective_tier 标记来源
    """
    keys = _tier_keys(tier)
    base_url = _resolve_string_value(rows.get(keys.base_url), keys.default_base_url)
    model = _resolve_string_value(rows.get(keys.model), keys.default_model)
    configured = bool(base_url.strip()) and bool(model.strip())

    if not configured and fallback is not None:
        return replace(fallback, configured=False)

    api_key, api_key_source = _resolve_api_key(
        rows.get(keys.api_key),
        keys.default_api_key,
    )
    # 读出来的值也要过一遍校验：数据库里的 base_url 不一定是通过 API 写进去的，
    # 不校验就等于拿一个未经检查的地址去发请求
    validated = ChatTierUpdate.model_validate(
        {
            "base_url": base_url,
            "model": model,
            "input_cost_per_million_usd": _resolve_float_value(
                rows.get(keys.input_cost),
                keys.default_input_cost,
            ),
            "output_cost_per_million_usd": _resolve_float_value(
                rows.get(keys.output_cost),
                keys.default_output_cost,
            ),
        }
    )
    return EffectiveChatTier(
        base_url=validated.base_url,
        api_key=api_key,
        api_key_source=api_key_source,
        model=validated.model,
        input_cost_per_million_usd=validated.input_cost_per_million_usd,
        output_cost_per_million_usd=validated.output_cost_per_million_usd,
        configured=configured,
        effective_tier=tier,
    )


async def get_effective_llm_config(db: AsyncSession) -> EffectiveLlmConfig:
    """
    读取并合并三档 chat 与 embedding 的有效配置。

    平衡档先解析，另外两档未配置时整份回退到它。平衡档自身不回退：
    它是回退的终点，即使数据库里没有行也会走 .env 兜底。

    Args:
        db: 异步数据库会话

    Returns:
        含解密后 API Key 的有效配置快照
    """
    rows = await system_config_crud.get_by_keys(db, LLM_CONFIG_KEYS)
    balanced = _resolve_chat_tier(rows, "balanced", fallback=None)
    embedding_api_key, embedding_api_key_source = _resolve_api_key(
        rows.get(KEY_LLM_EMBEDDING_API_KEY),
        settings.llm_embedding_api_key,
    )
    return EffectiveLlmConfig(
        chat_fast=_resolve_chat_tier(rows, "fast", fallback=balanced),
        chat_balanced=balanced,
        chat_strong=_resolve_chat_tier(rows, "strong", fallback=balanced),
        # 与三档同理：库里的地址也要校验后才拿去发请求
        embedding_base_url=normalize_base_url(
            _resolve_string_value(
                rows.get(KEY_LLM_EMBEDDING_BASE_URL),
                settings.LLM_EMBEDDING_BASE_URL,
            )
        ),
        embedding_api_key=embedding_api_key,
        embedding_api_key_source=embedding_api_key_source,
        embedding_model=_resolve_string_value(
            rows.get(KEY_LLM_EMBEDDING_MODEL),
            settings.LLM_EMBEDDING_MODEL,
        ),
    )


async def get_effective_operations_config(db: AsyncSession) -> EffectiveOperationsConfig:
    """
    读取并校验监控与 CMDB 巡检四项有效配置。

    Args:
        db: 异步数据库会话

    Returns:
        通过范围校验的有效运行参数快照
    """
    rows = await system_config_crud.get_by_keys(db, OPERATIONS_CONFIG_KEYS)
    validated = OperationsSystemConfigUpdate(
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
        monitor_probe_timeout_seconds=validated.monitor_probe_timeout_seconds,
        monitor_sweep_interval_seconds=validated.monitor_sweep_interval_seconds,
        cmdb_diff_interval_seconds=validated.cmdb_diff_interval_seconds,
        monitor_event_retention_days=validated.monitor_event_retention_days,
    )


def chat_tier_payload(payload: LlmSystemConfigUpdate, tier: ChatTier) -> ChatTierUpdate:
    """从更新载荷里取某一档的子模型。"""
    if tier == "fast":
        return payload.chat_fast
    if tier == "strong":
        return payload.chat_strong
    return payload.chat_balanced


def llm_config_plain_values(payload: LlmSystemConfigUpdate) -> dict[str, str]:
    """本次更新里所有**非密钥**项的目标值。

    保存与审计比对共用同一份：分开各写一遍的话，加一个配置项时漏掉审计那边
    不会报错，只会安静地少记一条变更。
    """
    values: dict[str, str] = {
        KEY_LLM_EMBEDDING_BASE_URL: payload.embedding_base_url,
        KEY_LLM_EMBEDDING_MODEL: payload.embedding_model,
    }
    for tier in CHAT_TIERS:
        tier_payload = chat_tier_payload(payload, tier)
        keys = _tier_keys(tier)
        values[keys.base_url] = tier_payload.base_url
        values[keys.model] = tier_payload.model
        values[keys.input_cost] = str(tier_payload.input_cost_per_million_usd)
        values[keys.output_cost] = str(tier_payload.output_cost_per_million_usd)
    return values


def chat_tier_api_key_action(tier_payload: ChatTierUpdate) -> str | None:
    """描述本次更新对某档 API Key 做了什么，供审计使用（不含 Key 内容）。"""
    if tier_payload.clear_api_key:
        return "已清空"
    if tier_payload.api_key is not None:
        return "已替换"
    return None


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
    values: dict[str, str | None] = dict(llm_config_plain_values(payload))
    # API Key 三态：清空标志 → 写 NULL；提交了新值 → 加密写入；
    # 两者都没有 → 这一档的 Key 不动，避免每次保存都要求重填密钥
    for tier in CHAT_TIERS:
        tier_payload = chat_tier_payload(payload, tier)
        api_key_key = _tier_keys(tier).api_key
        if tier_payload.clear_api_key:
            values[api_key_key] = None
        elif tier_payload.api_key is not None:
            values[api_key_key] = encrypt_secret(
                tier_payload.api_key.get_secret_value()
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


def _tier_response(tier: EffectiveChatTier) -> ChatTierResponse:
    """把一档有效配置转成脱敏响应（只暴露 Key 是否配置，不暴露 Key）。"""
    return ChatTierResponse(
        base_url=tier.base_url,
        model=tier.model,
        input_cost_per_million_usd=tier.input_cost_per_million_usd,
        output_cost_per_million_usd=tier.output_cost_per_million_usd,
        api_key_configured=tier.api_key_configured,
        api_key_source=tier.api_key_source,
        configured=tier.configured,
        effective_tier=tier.effective_tier,
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
            chat_fast=_tier_response(llm.chat_fast),
            chat_balanced=_tier_response(llm.chat_balanced),
            chat_strong=_tier_response(llm.chat_strong),
            embedding_base_url=llm.embedding_base_url,
            embedding_model=llm.embedding_model,
            embedding_api_key_configured=llm.embedding_api_key_configured,
            embedding_api_key_source=llm.embedding_api_key_source,
        ),
        operations=OperationsSystemConfigResponse(
            monitor_probe_timeout_seconds=operations.monitor_probe_timeout_seconds,
            monitor_sweep_interval_seconds=operations.monitor_sweep_interval_seconds,
            cmdb_diff_interval_seconds=operations.cmdb_diff_interval_seconds,
            monitor_event_retention_days=operations.monitor_event_retention_days,
        ),
    )
