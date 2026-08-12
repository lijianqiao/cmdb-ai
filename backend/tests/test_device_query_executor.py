"""DeviceQueryExecutor：凭据解析分支 + 输出截断 + 失败分类，不接真实设备。"""

from unittest.mock import AsyncMock, patch

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

    fake_connection = AsyncMock()
    fake_connection.send_command = AsyncMock(
        return_value=type("Resp", (), {"result": "Cisco IOS XE Software", "failed": False})()
    )
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
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
        "app.agent.executors._open_scrapli_connection",
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
    fake_connection = AsyncMock()
    fake_connection.send_command = AsyncMock(
        return_value=type("Resp", (), {"result": long_output, "failed": False})()
    )
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        executor = DeviceQueryExecutor()
        result = await executor.execute(
            db_session, asset=asset, command_name="show_version", dynamic_password=None
        )

    assert result.detail["truncated"] is True
    assert len(result.detail["output"]) < 10_000


def _generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()
