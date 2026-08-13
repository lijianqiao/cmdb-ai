"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_executors.py
@DateTime: 2026-08-12
@Docs: T10 HITL 执行器单元测试（notify + DeviceQueryExecutor 管控分支）。
"""

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.executors import DeviceQueryExecutor, NotifyExecutor
from app.core.cmdb_credential import encrypt_credential_password
from app.core.config import settings
from app.crud.cmdb_asset import cmdb_asset_crud
from app.models.audit_log import AuditLog
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_asset(
    db_session: AsyncSession,
    *,
    credential_type: str = "static",
    credential_username: str = "admin",
    credential_password_encrypted: str | None = "placeholder",
    vendor: str = "cisco_iosxe",
) -> object:
    """创建带厂商与凭据的交换机资产，供管控命令执行测试使用。"""
    created = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": "sw-exec-ctrl",
            "ip_address": "10.0.0.97",
            "vendor": vendor,
            "credential_type": credential_type,
            "credential_username": credential_username,
            "credential_password_encrypted": credential_password_encrypted,
        },
    )
    await db_session.flush()
    return created


def _generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


async def test_device_query_executor_reboot_sends_interactive_confirmation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reboot 命令要用 send_interactive 而不是 send_command。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="cisco_iosxe",
    )
    executor = DeviceQueryExecutor()
    fake_connection = AsyncMock()
    fake_connection.send_interactive = AsyncMock(
        return_value=type("Resp", (), {"result": "System will reboot", "failed": False})()
    )
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        result = await executor.execute(
            db_session, asset=asset, command_name="reboot", dynamic_password=None
        )
    assert result.ok is True
    fake_connection.send_interactive.assert_awaited_once()
    fake_connection.send_command.assert_not_called()


async def test_device_query_executor_connection_drop_during_reboot_is_conservative_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2：连接在重启命令执行中断开，不得伪造成功，必须提示人工核实。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="cisco_iosxe",
    )
    executor = DeviceQueryExecutor()
    fake_connection = AsyncMock()
    fake_connection.send_interactive = AsyncMock(side_effect=ConnectionError("closed"))
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        result = await executor.execute(
            db_session, asset=asset, command_name="reboot", dynamic_password=None
        )
    assert result.ok is False
    assert "人工核实" in result.message


async def test_device_query_executor_port_disable_uses_send_configs_with_interface(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """port_disable 走 send_configs，接口名要正确代入模板。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="cisco_iosxe",
    )
    executor = DeviceQueryExecutor()
    fake_connection = AsyncMock()
    fake_response_item = type("R", (), {"result": "ok", "failed": False})()
    fake_connection.send_configs = AsyncMock(return_value=[fake_response_item, fake_response_item])
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        result = await executor.execute(
            db_session,
            asset=asset,
            command_name="port_disable",
            dynamic_password=None,
            interface_name="GigabitEthernet0/1",
        )
    assert result.ok is True
    sent_lines = fake_connection.send_configs.call_args.args[0]
    assert sent_lines == ["interface GigabitEthernet0/1", "shutdown"]


async def test_device_query_executor_rejects_invalid_interface_name_before_connecting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法接口名必须在建立设备连接之前就拒绝，不能把它当命令片段发出去。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="cisco_iosxe",
    )
    executor = DeviceQueryExecutor()
    with patch("app.agent.executors._open_scrapli_connection") as mock_connect:
        result = await executor.execute(
            db_session,
            asset=asset,
            command_name="port_disable",
            dynamic_password=None,
            interface_name="eth0; reload",
        )
    assert result.ok is False
    mock_connect.assert_not_called()


async def test_device_query_executor_rejects_unsupported_vendor_before_connecting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """厂商不支持这条命令时，同样要在连接设备之前就失败（不是连接后才发现）。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="cisco_iosxe",
    )
    asset.vendor = "hp_comware"
    executor = DeviceQueryExecutor()
    with patch("app.agent.executors._open_scrapli_connection") as mock_connect:
        result = await executor.execute(
            db_session,
            asset=asset,
            command_name="port_disable",
            dynamic_password=None,
            interface_name="GigabitEthernet0/1",
        )
    assert result.ok is False
    assert result.message == "该设备厂商不支持这个命令"
    mock_connect.assert_not_called()


async def test_notify_executor_writes_audit_and_succeeds(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    executor = NotifyExecutor()
    proposal_id = 42

    result = await executor.execute(
        db_session,
        proposal_id=proposal_id,
        payload={"message": "SW-12 离线"},
        actor_user_id=test_user.id,
    )
    await db_session.flush()

    assert result.ok is True
    logs = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "hitl_notify_executed")
        )
    ).scalars().all()
    assert len(logs) == 1
    assert logs[0].user_id == test_user.id
    assert logs[0].target == f"hitl_proposal:{proposal_id}"
    assert "SW-12 离线" in logs[0].detail


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": ""},
        {"message": "   "},
    ],
)
async def test_notify_executor_rejects_blank_message(
    db_session: AsyncSession,
    test_user: User,
    payload: dict[str, str],
) -> None:
    executor = NotifyExecutor()

    result = await executor.execute(
        db_session,
        proposal_id=1,
        payload=payload,
        actor_user_id=test_user.id,
    )
    await db_session.flush()

    assert result.ok is False
    logs = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "hitl_notify_executed")
        )
    ).scalars().all()
    assert logs == []
