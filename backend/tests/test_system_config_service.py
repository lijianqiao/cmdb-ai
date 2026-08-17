"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_system_config_service.py
@DateTime: 2026-08-13 12:55
@Docs: 系统配置服务层：来源优先级、校验与脱敏测试。
"""

import math

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.data_encryption import encrypt_secret
from app.crud.system_config import system_config_crud
from app.schemas.system_config import (
    ChatTierResponse,
    LlmSystemConfigResponse,
    LlmSystemConfigUpdate,
    OperationsSystemConfigUpdate,
)
from app.services.system_config import (
    build_system_config_response,
    get_effective_llm_config,
    get_effective_operations_config,
    save_llm_config,
    save_operations_config,
)


def _tier_payload(**overrides: object) -> dict[str, object]:
    """一档 chat 配置的最小合法载荷。"""
    payload: dict[str, object] = {
        "base_url": "https://llm.example/v1",
        "model": "chat-model",
        "input_cost_per_million_usd": 0.0,
        "output_cost_per_million_usd": 0.0,
    }
    payload.update(overrides)
    return payload


def _llm_update_payload(**overrides: object) -> LlmSystemConfigUpdate:
    """默认只配平衡档，便宜档与强档留空（即"未配置"，运行时回退）。"""
    payload: dict[str, object] = {
        "chat_balanced": _tier_payload(),
        "embedding_base_url": "https://embedding.example/v1",
        "embedding_model": "embedding-model",
    }
    payload.update(overrides)
    return LlmSystemConfigUpdate.model_validate(payload)


@pytest.mark.asyncio
async def test_database_value_overrides_environment_fallback(
    db_session: AsyncSession,
) -> None:
    await system_config_crud.upsert_values(
        db_session,
        {
            "LLM_CHAT_BASE_URL": "https://db-llm.example/v1",
            "LLM_CHAT_MODEL": "db-chat-model",
        },
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)
    assert config.chat_balanced.base_url == "https://db-llm.example/v1"
    assert config.chat_balanced.model == "db-chat-model"


@pytest.mark.asyncio
async def test_explicit_null_api_key_blocks_environment_fallback(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "LLM_CHAT_API_KEY",
        SecretStr("env-chat-key-should-not-apply"),
    )
    await system_config_crud.upsert_values(
        db_session,
        {"LLM_CHAT_API_KEY": None},
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)
    assert config.chat_balanced.api_key == ""
    assert config.chat_balanced.api_key_source == "database"
    assert config.chat_balanced.api_key_configured is False


@pytest.mark.asyncio
async def test_explicit_null_embedding_api_key_blocks_environment_fallback(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "LLM_EMBEDDING_API_KEY",
        SecretStr("env-embedding-key-should-not-apply"),
    )
    await system_config_crud.upsert_values(
        db_session,
        {"LLM_EMBEDDING_API_KEY": None},
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)
    assert config.embedding_api_key == ""
    assert config.embedding_api_key_source == "database"
    assert config.embedding_api_key_configured is False


@pytest.mark.asyncio
async def test_save_llm_config_without_api_key_fields_retains_existing_key(
    db_session: AsyncSession,
) -> None:
    encrypted = encrypt_secret("existing-chat-key")
    await system_config_crud.upsert_values(
        db_session,
        {"LLM_CHAT_API_KEY": encrypted},
        updated_by_user_id=None,
    )
    await save_llm_config(
        db_session,
        _llm_update_payload(
            chat_balanced=_tier_payload(
                base_url="https://updated.example/v1",
                model="updated-model",
            ),
        ),
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)
    assert config.chat_balanced.api_key == "existing-chat-key"
    assert config.chat_balanced.api_key_source == "database"
    assert config.chat_balanced.api_key_configured is True
    assert config.chat_balanced.base_url == "https://updated.example/v1"
    assert config.chat_balanced.model == "updated-model"


@pytest.mark.asyncio
async def test_get_effective_llm_config_rejects_invalid_db_base_url(
    db_session: AsyncSession,
) -> None:
    await system_config_crud.upsert_values(
        db_session,
        {"LLM_CHAT_BASE_URL": "ftp://bad-host/v1"},
        updated_by_user_id=None,
    )
    with pytest.raises(ValidationError):
        await get_effective_llm_config(db_session)


@pytest.mark.asyncio
async def test_save_and_read_operations_config_round_trip(
    db_session: AsyncSession,
) -> None:
    payload = OperationsSystemConfigUpdate(
        monitor_probe_timeout_seconds=5.0,
        monitor_sweep_interval_seconds=60.0,
        cmdb_diff_interval_seconds=7200.0,
        monitor_event_retention_days=14,
    )
    await save_operations_config(
        db_session,
        payload,
        updated_by_user_id=None,
    )
    config = await get_effective_operations_config(db_session)
    assert config.monitor_probe_timeout_seconds == 5.0
    assert config.monitor_sweep_interval_seconds == 60.0
    assert config.cmdb_diff_interval_seconds == 7200.0
    assert config.monitor_event_retention_days == 14


@pytest.mark.asyncio
async def test_get_effective_operations_config_rejects_invalid_db_timeout(
    db_session: AsyncSession,
) -> None:
    await system_config_crud.upsert_values(
        db_session,
        {"MONITOR_PROBE_TIMEOUT_SECONDS": "0"},
        updated_by_user_id=None,
    )
    with pytest.raises(ValidationError):
        await get_effective_operations_config(db_session)


@pytest.mark.asyncio
async def test_environment_fallback_used_when_database_row_missing(
    db_session: AsyncSession,
) -> None:
    config = await get_effective_llm_config(db_session)
    assert config.chat_balanced.base_url == settings.LLM_CHAT_BASE_URL.rstrip("/")
    assert config.chat_balanced.api_key_source in {"environment", "unset"}


@pytest.mark.asyncio
async def test_database_encrypted_api_key_is_decrypted(
    db_session: AsyncSession,
) -> None:
    encrypted = encrypt_secret("db-chat-key")
    await system_config_crud.upsert_values(
        db_session,
        {"LLM_CHAT_API_KEY": encrypted},
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)
    assert config.chat_balanced.api_key == "db-chat-key"
    assert config.chat_balanced.api_key_source == "database"
    assert config.chat_balanced.api_key_configured is True


@pytest.mark.asyncio
async def test_response_never_exposes_api_key_plaintext(
    db_session: AsyncSession,
) -> None:
    secret = "sk-do-not-leak-this"
    await save_llm_config(
        db_session,
        _llm_update_payload(
            chat_balanced=_tier_payload(api_key=SecretStr(secret)),
        ),
        updated_by_user_id=None,
    )
    response = await build_system_config_response(db_session)
    dumped = response.model_dump_json()
    assert secret not in dumped
    assert response.llm.chat_balanced.api_key_configured is True
    # 三档的响应模型共用 ChatTierResponse，它没有 api_key 字段就等于三档都不会泄露
    assert "api_key" not in ChatTierResponse.model_fields
    assert "chat_api_key" not in LlmSystemConfigResponse.model_fields


def _invalid_llm_payload(**tier_overrides: object) -> dict[str, object]:
    """构造一份只有平衡档有问题的载荷。"""
    return {
        "chat_balanced": _tier_payload(**tier_overrides),
        "embedding_base_url": "https://embedding.example/v1",
        "embedding_model": "e",
    }


@pytest.mark.parametrize(
    ("payload", "expected_loc"),
    [
        (
            _invalid_llm_payload(base_url="ftp://host/v1"),
            ("chat_balanced", "base_url"),
        ),
        (
            _invalid_llm_payload(base_url="https://user:password@host/v1"),
            ("chat_balanced", "base_url"),
        ),
        (
            _invalid_llm_payload(input_cost_per_million_usd=-1.0),
            ("chat_balanced", "input_cost_per_million_usd"),
        ),
        (
            _invalid_llm_payload(input_cost_per_million_usd=math.nan),
            ("chat_balanced", "input_cost_per_million_usd"),
        ),
        (
            _invalid_llm_payload(output_cost_per_million_usd=math.inf),
            ("chat_balanced", "output_cost_per_million_usd"),
        ),
        (
            _invalid_llm_payload(api_key="sk-test", clear_api_key=True),
            (),
        ),
        # 平衡档是回退目标，留空必须被拒——空了就没有任何一档可用
        (
            {
                "chat_balanced": _tier_payload(base_url="", model=""),
                "embedding_base_url": "https://embedding.example/v1",
                "embedding_model": "e",
            },
            (),
        ),
    ],
)
def test_llm_update_rejects_invalid_values(
    payload: dict[str, object],
    expected_loc: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        LlmSystemConfigUpdate.model_validate(payload)
    if expected_loc:
        assert exc_info.value.errors()[0]["loc"] == expected_loc


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("monitor_probe_timeout_seconds", 0),
        ("monitor_probe_timeout_seconds", 31),
        ("monitor_sweep_interval_seconds", 4),
        ("monitor_sweep_interval_seconds", 3601),
        ("cmdb_diff_interval_seconds", 59),
        ("cmdb_diff_interval_seconds", 86401),
        ("monitor_event_retention_days", 0),
        ("monitor_event_retention_days", 91),
    ],
)
def test_operations_update_rejects_out_of_range_values(
    field: str,
    value: float | int,
) -> None:
    payload = {
        "monitor_probe_timeout_seconds": 3.0,
        "monitor_sweep_interval_seconds": 30.0,
        "cmdb_diff_interval_seconds": 3600.0,
        "monitor_event_retention_days": 7,
        field: value,
    }
    with pytest.raises(ValidationError):
        OperationsSystemConfigUpdate.model_validate(payload)


def test_secret_str_repr_does_not_leak_plaintext() -> None:
    secret = SecretStr("sk-hidden-value")
    assert "sk-hidden-value" not in repr(secret)


@pytest.mark.asyncio
async def test_unconfigured_tier_falls_back_to_balanced_including_prices(
    db_session: AsyncSession,
) -> None:
    """便宜档没配时整份回退到平衡档，**两个单价也要跟着回退**。

    只回退连接信息、保留本档单价的话，会按便宜档的价格给平衡档的调用记账，
    界面上的花费数字就是编的。
    """
    await system_config_crud.upsert_values(
        db_session,
        {
            "LLM_CHAT_BASE_URL": "https://balanced.example/v1",
            "LLM_CHAT_MODEL": "balanced-model",
            "LLM_CHAT_INPUT_COST_PER_MILLION_USD": "3.0",
            "LLM_CHAT_OUTPUT_COST_PER_MILLION_USD": "9.0",
        },
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)

    assert config.chat_fast.configured is False
    assert config.chat_fast.effective_tier == "balanced"
    assert config.chat_fast.base_url == "https://balanced.example/v1"
    assert config.chat_fast.model == "balanced-model"
    assert config.chat_fast.input_cost_per_million_usd == 3.0
    assert config.chat_fast.output_cost_per_million_usd == 9.0
    assert config.chat_strong.configured is False


@pytest.mark.asyncio
async def test_configured_tier_keeps_its_own_settings(
    db_session: AsyncSession,
) -> None:
    """三档都配置时各取各的，不互相污染。"""
    await system_config_crud.upsert_values(
        db_session,
        {
            "LLM_CHAT_BASE_URL": "https://balanced.example/v1",
            "LLM_CHAT_MODEL": "balanced-model",
            "LLM_CHAT_FAST_BASE_URL": "https://fast.example/v1",
            "LLM_CHAT_FAST_MODEL": "fast-model",
            "LLM_CHAT_FAST_INPUT_COST_PER_MILLION_USD": "0.1",
            "LLM_CHAT_STRONG_BASE_URL": "https://strong.example/v1",
            "LLM_CHAT_STRONG_MODEL": "strong-model",
        },
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)

    assert config.chat_fast.configured is True
    assert config.chat_fast.effective_tier == "fast"
    assert config.chat_fast.base_url == "https://fast.example/v1"
    assert config.chat_fast.input_cost_per_million_usd == 0.1
    assert config.chat_strong.model == "strong-model"
    assert config.chat_balanced.model == "balanced-model"


@pytest.mark.asyncio
async def test_half_configured_tier_counts_as_unconfigured(
    db_session: AsyncSession,
) -> None:
    """只填了 base_url 没填模型名 → 判为未配置。半份配置发不出请求。"""
    await system_config_crud.upsert_values(
        db_session,
        {
            "LLM_CHAT_BASE_URL": "https://balanced.example/v1",
            "LLM_CHAT_MODEL": "balanced-model",
            "LLM_CHAT_STRONG_BASE_URL": "https://strong.example/v1",
        },
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)

    assert config.chat_strong.configured is False
    assert config.chat_strong.base_url == "https://balanced.example/v1"


@pytest.mark.asyncio
async def test_tier_api_key_is_independent(db_session: AsyncSession) -> None:
    """各档密钥互不干扰：清空强档不影响平衡档。"""
    await save_llm_config(
        db_session,
        _llm_update_payload(
            chat_balanced=_tier_payload(api_key=SecretStr("balanced-key")),
            chat_strong=_tier_payload(
                base_url="https://strong.example/v1",
                model="strong-model",
                api_key=SecretStr("strong-key"),
            ),
        ),
        updated_by_user_id=None,
    )
    await save_llm_config(
        db_session,
        _llm_update_payload(
            chat_strong=_tier_payload(
                base_url="https://strong.example/v1",
                model="strong-model",
                clear_api_key=True,
            ),
        ),
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)

    assert config.chat_balanced.api_key == "balanced-key"
    assert config.chat_strong.api_key == ""
