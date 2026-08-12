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


def _llm_update_payload(**overrides: object) -> LlmSystemConfigUpdate:
    payload = {
        "chat_base_url": "https://llm.example/v1",
        "chat_model": "chat-model",
        "chat_input_cost_per_million_usd": 0.0,
        "chat_output_cost_per_million_usd": 0.0,
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
    assert config.chat_base_url == "https://db-llm.example/v1"
    assert config.chat_model == "db-chat-model"


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
    assert config.chat_api_key == ""
    assert config.chat_api_key_source == "database"
    assert config.chat_api_key_configured is False


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
            chat_base_url="https://updated.example/v1",
            chat_model="updated-model",
        ),
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)
    assert config.chat_api_key == "existing-chat-key"
    assert config.chat_api_key_source == "database"
    assert config.chat_api_key_configured is True
    assert config.chat_base_url == "https://updated.example/v1"
    assert config.chat_model == "updated-model"


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
        hitl_notify_auto_approve=True,
        monitor_probe_timeout_seconds=5.0,
        monitor_sweep_interval_seconds=60.0,
        cmdb_diff_interval_seconds=7200.0,
    )
    await save_operations_config(
        db_session,
        payload,
        updated_by_user_id=None,
    )
    config = await get_effective_operations_config(db_session)
    assert config.hitl_notify_auto_approve is True
    assert config.monitor_probe_timeout_seconds == 5.0
    assert config.monitor_sweep_interval_seconds == 60.0
    assert config.cmdb_diff_interval_seconds == 7200.0


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
    assert config.chat_base_url == settings.LLM_CHAT_BASE_URL.rstrip("/")
    assert config.chat_api_key_source in {"environment", "unset"}


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
    assert config.chat_api_key == "db-chat-key"
    assert config.chat_api_key_source == "database"
    assert config.chat_api_key_configured is True


@pytest.mark.asyncio
async def test_response_never_exposes_api_key_plaintext(
    db_session: AsyncSession,
) -> None:
    secret = "sk-do-not-leak-this"
    await save_llm_config(
        db_session,
        _llm_update_payload(
            chat_base_url="https://llm.example/v1",
            chat_api_key=SecretStr(secret),
        ),
        updated_by_user_id=None,
    )
    response = await build_system_config_response(db_session)
    dumped = response.model_dump_json()
    assert secret not in dumped
    assert response.llm.chat_api_key_configured is True
    assert "chat_api_key" not in LlmSystemConfigResponse.model_fields


@pytest.mark.parametrize(
    ("payload", "expected_loc"),
    [
        (
            {
                "chat_base_url": "ftp://host/v1",
                "chat_model": "m",
                "chat_input_cost_per_million_usd": 0.0,
                "chat_output_cost_per_million_usd": 0.0,
                "embedding_base_url": "https://embedding.example/v1",
                "embedding_model": "e",
            },
            ("chat_base_url",),
        ),
        (
            {
                "chat_base_url": "https://user:password@host/v1",
                "chat_model": "m",
                "chat_input_cost_per_million_usd": 0.0,
                "chat_output_cost_per_million_usd": 0.0,
                "embedding_base_url": "https://embedding.example/v1",
                "embedding_model": "e",
            },
            ("chat_base_url",),
        ),
        (
            {
                "chat_base_url": "https://chat.example/v1",
                "chat_model": "m",
                "chat_input_cost_per_million_usd": -1.0,
                "chat_output_cost_per_million_usd": 0.0,
                "embedding_base_url": "https://embedding.example/v1",
                "embedding_model": "e",
            },
            ("chat_input_cost_per_million_usd",),
        ),
        (
            {
                "chat_base_url": "https://chat.example/v1",
                "chat_model": "m",
                "chat_input_cost_per_million_usd": math.nan,
                "chat_output_cost_per_million_usd": 0.0,
                "embedding_base_url": "https://embedding.example/v1",
                "embedding_model": "e",
            },
            ("chat_input_cost_per_million_usd",),
        ),
        (
            {
                "chat_base_url": "https://chat.example/v1",
                "chat_model": "m",
                "chat_input_cost_per_million_usd": 0.0,
                "chat_output_cost_per_million_usd": math.inf,
                "embedding_base_url": "https://embedding.example/v1",
                "embedding_model": "e",
            },
            ("chat_output_cost_per_million_usd",),
        ),
        (
            {
                "chat_base_url": "https://chat.example/v1",
                "chat_model": "m",
                "chat_input_cost_per_million_usd": 0.0,
                "chat_output_cost_per_million_usd": 0.0,
                "embedding_base_url": "https://embedding.example/v1",
                "embedding_model": "e",
                "chat_api_key": "sk-test",
                "clear_chat_api_key": True,
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
    ],
)
def test_operations_update_rejects_out_of_range_values(
    field: str,
    value: float,
) -> None:
    payload = {
        "hitl_notify_auto_approve": False,
        "monitor_probe_timeout_seconds": 3.0,
        "monitor_sweep_interval_seconds": 30.0,
        "cmdb_diff_interval_seconds": 3600.0,
        field: value,
    }
    with pytest.raises(ValidationError):
        OperationsSystemConfigUpdate.model_validate(payload)


def test_secret_str_repr_does_not_leak_plaintext() -> None:
    secret = SecretStr("sk-hidden-value")
    assert "sk-hidden-value" not in repr(secret)
