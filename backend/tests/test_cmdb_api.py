"""CMDB 资产管理 API：CRUD、回收站、以及凭据永不回显明文的安全测试。"""

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _grant_cmdb_permissions(db_session: AsyncSession, test_user) -> None:  # noqa: ANN001
    """现场创建 cmdb:read + cmdb:manage 并挂到 test_user 已有角色上。

    不能像别处那样直接查询已存在的 Permission 行——测试库的权限种子只来自
    conftest.py 的 test_permissions fixture（user/role/permission/audit 相关），
    不包含 init_db.py 里的 cmdb:* 种子数据，那些只在真实启动时跑。这里照抄
    tests/test_hitl_api.py::_grant_hitl_approve 的"现场创建"模式，而不是
    tests/test_hitl_integration.py 早期版本里错误示范过的"查询已有行"模式。
    """
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


async def test_create_asset_with_static_credential_never_echoes_plaintext(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    secret = "Sup3rSecretDevicePwd!"

    response = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-api-01",
            "ip_address": "10.0.9.1",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password": secret,
        },
        headers=auth_headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["credential_password_set"] is True
    assert secret not in response.text
    assert "credential_password_encrypted" not in response.text


async def test_create_dynamic_credential_stores_username_only(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)

    response = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-api-02",
            "ip_address": "10.0.9.2",
            "credential_type": "dynamic",
            "credential_username": "otp-admin",
        },
        headers=auth_headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["credential_type"] == "dynamic"
    assert body["credential_username"] == "otp-admin"
    assert body["credential_password_set"] is False


async def test_update_without_password_keeps_existing_secret(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-api-03",
            "ip_address": "10.0.9.3",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password": "orig-pwd",
        },
        headers=auth_headers,
    )
    asset_id = create_resp.json()["data"]["id"]

    update_resp = await client.patch(
        f"/api/v1/cmdb/assets/{asset_id}",
        json={"hostname": "srv-api-03-renamed"},
        headers=auth_headers,
    )
    assert update_resp.status_code == 200, update_resp.text
    body = update_resp.json()["data"]
    assert body["hostname"] == "srv-api-03-renamed"
    assert body["credential_password_set"] is True  # 没碰凭据字段，密文原样保留


async def test_switch_to_static_without_password_is_rejected_when_no_existing_secret(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={"asset_type": "server", "hostname": "srv-api-04", "ip_address": "10.0.9.4"},
        headers=auth_headers,
    )
    asset_id = create_resp.json()["data"]["id"]

    response = await client.patch(
        f"/api/v1/cmdb/assets/{asset_id}",
        json={"credential_type": "static", "credential_username": "admin"},
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


async def test_soft_delete_restore_purge_flow(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={"asset_type": "server", "hostname": "srv-api-05", "ip_address": "10.0.9.5"},
        headers=auth_headers,
    )
    asset_id = create_resp.json()["data"]["id"]

    delete_resp = await client.delete(f"/api/v1/cmdb/assets/{asset_id}", headers=auth_headers)
    assert delete_resp.status_code == 200, delete_resp.text

    get_resp = await client.get(f"/api/v1/cmdb/assets/{asset_id}", headers=auth_headers)
    assert get_resp.status_code == 404

    deleted_resp = await client.get("/api/v1/cmdb/assets/deleted", headers=auth_headers)
    assert deleted_resp.status_code == 200
    assert any(item["id"] == asset_id for item in deleted_resp.json()["data"]["items"])

    restore_resp = await client.post(f"/api/v1/cmdb/assets/{asset_id}/restore", headers=auth_headers)
    assert restore_resp.status_code == 200, restore_resp.text

    delete_again = await client.delete(f"/api/v1/cmdb/assets/{asset_id}", headers=auth_headers)
    assert delete_again.status_code == 200
    purge_resp = await client.delete(f"/api/v1/cmdb/assets/{asset_id}/purge", headers=auth_headers)
    assert purge_resp.status_code == 200, purge_resp.text

    purge_again = await client.delete(f"/api/v1/cmdb/assets/{asset_id}/purge", headers=auth_headers)
    assert purge_again.status_code == 404


async def test_read_only_role_cannot_create(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    # test_user 默认没有任何 cmdb 权限（未调用 _grant_cmdb_permissions）
    response = await client.post(
        "/api/v1/cmdb/assets",
        json={"asset_type": "server", "hostname": "srv-forbidden", "ip_address": "10.0.9.9"},
        headers=auth_headers,
    )
    assert response.status_code == 403
