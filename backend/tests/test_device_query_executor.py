"""DeviceQueryExecutor：凭据解析分支 + 输出截断 + 失败分类，不接真实设备。"""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.executors import DeviceQueryExecutor
from app.core.cmdb_credential import encrypt_credential_password
from app.core.config import settings
from app.crud.cmdb_asset import cmdb_asset_crud

pytestmark = pytest.mark.asyncio


async def _make_asset(
    db_session: AsyncSession, *, credential_type: str, credential_username: str = "",
    credential_password_encrypted: str | None = None,
) -> int:
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": "sw-exec-01",
            "ip_address": "10.0.0.98",
            "vendor": "cisco_iosxe",
            "credential_type": credential_type,
            "credential_username": credential_username,
            "credential_password_encrypted": credential_password_encrypted,
        },
    )
    await db_session.flush()
    return asset.id


async def test_dynamic_credential_without_password_fails_closed(
    db_session: AsyncSession,
) -> None:
    asset_id = await _make_asset(db_session, credential_type="dynamic", credential_username="admin")
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    executor = DeviceQueryExecutor()
    result = await executor.execute(
        db_session, asset=asset, command_name="show_version", dynamic_password=None
    )

    assert result.ok is False


async def test_static_credential_decrypts_and_connects(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    secret = "Sup3rSecret!"
    ciphertext = encrypt_credential_password(secret)
    asset_id = await _make_asset(
        db_session,
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted=ciphertext,
    )
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value="Cisco IOS XE Software")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        executor = DeviceQueryExecutor()
        result = await executor.execute(
            db_session, asset=asset, command_name="show_version", dynamic_password=None
        )

    assert result.ok is True
    assert "Cisco IOS XE" in result.detail["output"]


async def test_connection_failure_does_not_leak_raw_exception_text(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_asset(
        db_session,
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted=ciphertext,
    )
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    with patch(
        "app.agent.executors._open_netmiko_connection",
        side_effect=RuntimeError("internal topology detail: 10.9.9.9 refused"),
    ):
        executor = DeviceQueryExecutor()
        result = await executor.execute(
            db_session, asset=asset, command_name="show_version", dynamic_password=None
        )

    assert result.ok is False
    assert "10.9.9.9" not in result.message


async def test_long_output_is_truncated(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_asset(
        db_session,
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted=ciphertext,
    )
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    long_output = "x" * 10_000
    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value=long_output)
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        executor = DeviceQueryExecutor()
        result = await executor.execute(
            db_session, asset=asset, command_name="show_version", dynamic_password=None
        )

    assert result.detail["truncated"] is True
    assert len(result.detail["output"]) < 10_000


async def test_unknown_command_name_gives_specific_message(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_asset(
        db_session,
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted=ciphertext,
    )
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    executor = DeviceQueryExecutor()
    result = await executor.execute(
        db_session, asset=asset, command_name="drop_table", dynamic_password=None
    )

    assert result.ok is False
    assert result.message == "未知命令名"


async def test_vendor_unsupported_command_gives_specific_message(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_asset(
        db_session,
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted=ciphertext,
    )
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None
    asset.vendor = "linux"  # show_running_config 目录里没有 linux 模板

    executor = DeviceQueryExecutor()
    result = await executor.execute(
        db_session, asset=asset, command_name="show_running_config", dynamic_password=None
    )

    assert result.ok is False
    assert result.message == "该设备厂商不支持这个命令"
    assert result.dispatched is False


async def test_connect_failure_reports_not_dispatched(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连接建不起来时必须报告 dispatched=False，让上层能安全回退重试。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("pw")
    asset_id = await _make_asset(
        db_session,
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted=ciphertext,
    )
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    executor = DeviceQueryExecutor()
    with patch(
        "app.agent.executors._open_netmiko_connection",
        side_effect=ConnectionError("unreachable"),
    ):
        result = await executor.execute(
            db_session, asset=asset, command_name="show_version", dynamic_password=None
        )

    assert result.ok is False
    assert result.dispatched is False
    assert result.detail["error_class"] == "ConnectionError"


async def test_send_failure_after_connect_reports_dispatched(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连接已建立后失败必须报告 dispatched=True，上层只能走 UNKNOWN 人工核实。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("pw")
    asset_id = await _make_asset(
        db_session,
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted=ciphertext,
    )
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    broken = MagicMock()
    broken.send_command = MagicMock(side_effect=ConnectionError("dropped mid-command"))
    executor = DeviceQueryExecutor()
    with patch("app.agent.executors._open_netmiko_connection", return_value=broken):
        result = await executor.execute(
            db_session, asset=asset, command_name="show_version", dynamic_password=None
        )

    assert result.ok is False
    assert result.dispatched is True
    assert result.detail["error_class"] == "ConnectionError"


async def test_conn_and_read_timeouts_are_passed_separately(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """建连超时与读超时量纲不同，必须各传各的值，不能共用一个配置项。

    conn_timeout 管建连/认证，read_timeout 管单条命令等提示符——show running-config
    这类大输出全靠后者兜底，混用会让其中一个被另一个的量纲压死。
    """
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    monkeypatch.setattr(settings, "DEVICE_COMMAND_CONN_TIMEOUT_SECONDS", 11.0)
    monkeypatch.setattr(settings, "DEVICE_COMMAND_READ_TIMEOUT_SECONDS", 33.0)
    ciphertext = encrypt_credential_password("pw")
    asset_id = await _make_asset(
        db_session,
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted=ciphertext,
    )
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    captured: dict[str, object] = {}
    conn = MagicMock()
    conn.send_command = MagicMock(return_value="ok")

    def fake_open(**kwargs: object) -> MagicMock:
        captured.update(kwargs)
        return conn

    with patch("app.agent.executors._open_netmiko_connection", side_effect=fake_open):
        result = await DeviceQueryExecutor().execute(
            db_session, asset=asset, command_name="show_version", dynamic_password=None
        )

    assert result.ok is True
    assert captured["conn_timeout"] == 11.0
    assert conn.send_command.call_args.kwargs["read_timeout"] == 33.0


def _generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()
