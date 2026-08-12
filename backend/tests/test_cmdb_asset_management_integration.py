"""CMDB 资产管理端到端验收：加密往返 + 全生命周期 + 密码永不泄露。"""

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _grant_cmdb_permissions(db_session: AsyncSession, test_user) -> None:  # noqa: ANN001
    """现场创建 cmdb:read + cmdb:manage 并挂到 test_user 已有角色上（同 Task 5 的写法）。"""
    from sqlalchemy import select

    from app.models.permission import Permission
    from app.models.role import role_permissions
    from app.models.user import user_roles

    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    for code, name in (("cmdb:read", "查看 CMDB 资产"), ("cmdb:manage", "管理 CMDB 资产")):
        permission = Permission(name=name, code=code, module="CMDB")
        db_session.add(permission)
        await db_session.flush()
        await db_session.execute(
            role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
        )
    await db_session.commit()


async def test_full_lifecycle_with_encrypted_credential_round_trips(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    await _grant_cmdb_permissions(db_session, test_user)
    secret = "IntegrationSecret!23"

    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "switch",
            "hostname": "sw-integration-01",
            "ip_address": "10.0.10.1",
            "vendor": "generic",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password": secret,
        },
        headers=auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    asset_id = create_resp.json()["data"]["id"]
    assert secret not in create_resp.text

    from app.core.cmdb_credential import decrypt_credential_password
    from app.crud.cmdb_asset import cmdb_asset_crud

    db_session.expire_all()
    row = await cmdb_asset_crud.get(db_session, asset_id)
    assert row is not None
    assert row.credential_password_encrypted is not None
    assert row.credential_password_encrypted != secret
    assert decrypt_credential_password(row.credential_password_encrypted) == secret

    switch_resp = await client.patch(
        f"/api/v1/cmdb/assets/{asset_id}",
        json={"credential_type": "dynamic", "credential_username": "otp-admin"},
        headers=auth_headers,
    )
    assert switch_resp.status_code == 200, switch_resp.text
    assert switch_resp.json()["data"]["credential_password_set"] is False

    db_session.expire_all()
    row = await cmdb_asset_crud.get(db_session, asset_id)
    assert row is not None
    assert row.credential_password_encrypted is None

    list_resp = await client.get("/api/v1/cmdb/assets", headers=auth_headers)
    assert list_resp.status_code == 200
    assert secret not in list_resp.text
    assert "credential_password_encrypted" not in list_resp.text

    delete_resp = await client.delete(f"/api/v1/cmdb/assets/{asset_id}", headers=auth_headers)
    assert delete_resp.status_code == 200

    restore_resp = await client.post(f"/api/v1/cmdb/assets/{asset_id}/restore", headers=auth_headers)
    assert restore_resp.status_code == 200
