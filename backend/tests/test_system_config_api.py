"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_system_config_api.py
@DateTime: 2026-08-13 13:05
@Docs: 系统配置 API：RBAC、脱敏、审计与事务原子性测试。
"""

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import system_config as system_config_routes
from app.core.config import settings
from app.core.data_encryption import encrypt_secret
from app.crud.system_config import system_config_crud
from app.models.audit_log import AuditLog
from app.models.permission import Permission
from app.models.system_config import SystemConfig
from app.models.user import User, user_roles

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _grant_system_config_manage(db_session: AsyncSession, test_user: User) -> None:
    """将 system_config:manage 挂到 test_user 已有角色上。"""
    from app.models.role import role_permissions

    permission = Permission(
        name="管理系统配置",
        code="system_config:manage",
        module="系统配置",
    )
    db_session.add(permission)
    await db_session.flush()

    role_id = (
        await db_session.execute(
            select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id)
        )
    ).scalar_one()
    await db_session.execute(
        role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
    )
    await db_session.commit()


def _llm_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "chat_base_url": "https://llm.example/v1",
        "clear_chat_api_key": False,
        "chat_model": "chat-model",
        "chat_input_cost_per_million_usd": 1.5,
        "chat_output_cost_per_million_usd": 2.5,
        "embedding_base_url": "https://embedding.example/v1",
        "clear_embedding_api_key": False,
        "embedding_model": "embedding-model",
    }
    payload.update(overrides)
    return payload


def _operations_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "hitl_notify_auto_approve": False,
        "monitor_probe_timeout_seconds": 3,
        "monitor_sweep_interval_seconds": 30,
        "cmdb_diff_interval_seconds": 3600,
        "monitor_event_retention_days": 7,
    }
    payload.update(overrides)
    return payload


async def test_unauthenticated_requests_return_401(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/system-config")).status_code == 401
    assert (
        await client.put(
            "/api/v1/system-config/llm",
            json=_llm_payload(),
        )
    ).status_code == 401
    assert (
        await client.put(
            "/api/v1/system-config/operations",
            json=_operations_payload(),
        )
    ).status_code == 401


async def test_regular_user_without_permission_cannot_read_or_update(
    client: AsyncClient,
    auth_headers: Headers,
) -> None:
    assert (
        await client.get("/api/v1/system-config", headers=auth_headers)
    ).status_code == 403
    assert (
        await client.put(
            "/api/v1/system-config/operations",
            headers=auth_headers,
            json=_operations_payload(),
        )
    ).status_code == 403


async def test_non_superuser_with_manage_permission_can_read_and_update(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    await _grant_system_config_manage(db_session, test_user)

    get_response = await client.get("/api/v1/system-config", headers=auth_headers)
    assert get_response.status_code == 200, get_response.text
    assert "llm" in get_response.json()["data"]
    assert "operations" in get_response.json()["data"]

    put_response = await client.put(
        "/api/v1/system-config/operations",
        headers=auth_headers,
        json=_operations_payload(hitl_notify_auto_approve=True),
    )
    assert put_response.status_code == 200, put_response.text
    assert put_response.json()["data"]["operations"]["hitl_notify_auto_approve"] is True


async def test_superuser_can_save_api_key_but_response_and_audit_are_redacted(
    client: AsyncClient,
    superuser_headers: Headers,
    db_session: AsyncSession,
) -> None:
    secret = "sk-do-not-return-this"
    response = await client.put(
        "/api/v1/system-config/llm",
        headers=superuser_headers,
        json=_llm_payload(chat_api_key=secret),
    )
    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert response.json()["data"]["llm"]["chat_api_key_configured"] is True

    db_session.expire_all()
    logs = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "update_llm_system_config")
        )
    ).scalars().all()
    assert logs
    assert secret not in logs[-1].detail


async def test_get_and_put_responses_set_cache_control_no_store(
    client: AsyncClient,
    superuser_headers: Headers,
) -> None:
    get_response = await client.get("/api/v1/system-config", headers=superuser_headers)
    assert get_response.status_code == 200
    assert get_response.headers.get("cache-control") == "no-store"

    put_response = await client.put(
        "/api/v1/system-config/operations",
        headers=superuser_headers,
        json=_operations_payload(),
    )
    assert put_response.status_code == 200
    assert put_response.headers.get("cache-control") == "no-store"


async def test_operations_update_rejects_out_of_range_values(
    client: AsyncClient,
    superuser_headers: Headers,
) -> None:
    response = await client.put(
        "/api/v1/system-config/operations",
        headers=superuser_headers,
        json=_operations_payload(monitor_probe_timeout_seconds=0),
    )
    assert response.status_code == 422


async def test_clear_chat_api_key_writes_explicit_null_row(
    client: AsyncClient,
    superuser_headers: Headers,
    db_session: AsyncSession,
) -> None:
    await system_config_crud.upsert_values(
        db_session,
        {"LLM_CHAT_API_KEY": encrypt_secret("to-be-cleared")},
        updated_by_user_id=None,
    )
    await db_session.commit()

    response = await client.put(
        "/api/v1/system-config/llm",
        headers=superuser_headers,
        json=_llm_payload(clear_chat_api_key=True),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["llm"]["chat_api_key_configured"] is False

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(SystemConfig).where(SystemConfig.key == "LLM_CHAT_API_KEY")
        )
    ).scalar_one()
    assert row.value is None


async def test_omitted_api_key_preserves_existing_ciphertext(
    client: AsyncClient,
    superuser_headers: Headers,
    db_session: AsyncSession,
) -> None:
    encrypted = encrypt_secret("existing-chat-key")
    await system_config_crud.upsert_values(
        db_session,
        {"LLM_CHAT_API_KEY": encrypted},
        updated_by_user_id=None,
    )
    await db_session.commit()

    response = await client.put(
        "/api/v1/system-config/llm",
        headers=superuser_headers,
        json=_llm_payload(chat_base_url="https://updated.example/v1"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["llm"]["chat_api_key_configured"] is True

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(SystemConfig).where(SystemConfig.key == "LLM_CHAT_API_KEY")
        )
    ).scalar_one()
    assert row.value == encrypted


async def test_operations_update_writes_audit_in_same_transaction(
    client: AsyncClient,
    superuser_headers: Headers,
    db_session: AsyncSession,
) -> None:
    response = await client.put(
        "/api/v1/system-config/operations",
        headers=superuser_headers,
        json=_operations_payload(monitor_sweep_interval_seconds=45),
    )
    assert response.status_code == 200, response.text

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(SystemConfig).where(
                SystemConfig.key == "MONITOR_SWEEP_INTERVAL_SECONDS"
            )
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.value == "45.0"

    logs = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "update_operations_system_config"
            )
        )
    ).scalars().all()
    assert logs
    assert logs[-1].target == "system_config:operations"


async def test_business_write_rolls_back_when_audit_write_fails(
    client: AsyncClient,
    superuser_headers: Headers,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(system_config_routes, "log_audit", fail_audit)
    response = await client.put(
        "/api/v1/system-config/operations",
        headers=superuser_headers,
        json=_operations_payload(cmdb_diff_interval_seconds=7200),
    )
    assert response.status_code == 500

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(SystemConfig).where(
                SystemConfig.key == "CMDB_DIFF_INTERVAL_SECONDS"
            )
        )
    ).scalar_one_or_none()
    assert row is None


async def test_missing_encryption_key_returns_422_on_llm_write(
    client: AsyncClient,
    superuser_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", None)
    response = await client.put(
        "/api/v1/system-config/llm",
        headers=superuser_headers,
        json=_llm_payload(chat_api_key="sk-new-key"),
    )
    assert response.status_code == 422
    assert "CMDB_CREDENTIAL_KEY" in response.text
    assert "sk-new-key" not in response.text


async def test_missing_encryption_key_returns_422_on_read(
    client: AsyncClient,
    superuser_headers: Headers,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ciphertext = encrypt_secret("sk-stored-key")
    await system_config_crud.upsert_values(
        db_session,
        {"LLM_CHAT_API_KEY": ciphertext},
        updated_by_user_id=None,
    )
    await db_session.commit()

    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", None)
    response = await client.get("/api/v1/system-config", headers=superuser_headers)
    assert response.status_code == 422
    assert "CMDB_CREDENTIAL_KEY" in response.text
    assert ciphertext not in response.text
    assert "sk-stored-key" not in response.text


async def test_decrypt_error_on_read_returns_500_without_ciphertext(
    client: AsyncClient,
    superuser_headers: Headers,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ciphertext = Fernet.generate_key().decode()
    await system_config_crud.upsert_values(
        db_session,
        {"LLM_CHAT_API_KEY": ciphertext},
        updated_by_user_id=None,
    )
    await db_session.commit()

    response = await client.get("/api/v1/system-config", headers=superuser_headers)
    assert response.status_code == 500
    assert ciphertext not in response.text
    assert "CMDB" in response.text or "cmdb" in response.text.lower()
