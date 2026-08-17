"""CMDB 资产管理 API：CRUD、回收站、以及凭据永不回显明文的安全测试。"""

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit_log import AuditLog

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _grant_cmdb_permissions(db_session: AsyncSession, test_user) -> None:
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


async def _latest_update_audit_detail(db_session: AsyncSession, asset_id: int) -> str:
    """取最近一次 update_cmdb_asset 审计记录的 detail 字段。"""
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(AuditLog)
            .where(
                AuditLog.action == "update_cmdb_asset",
                AuditLog.target == f"cmdb_asset:{asset_id}",
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    return row.detail


async def test_create_asset_with_static_credential_never_echoes_plaintext(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,
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
            "vendor": "generic",
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
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)

    response = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-api-02",
            "ip_address": "10.0.9.2",
            "vendor": "generic",
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
    test_user,
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
            "vendor": "generic",
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


async def test_update_hostname_with_unchanged_credentials_audit_not_changed(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟前端编辑弹窗每次都会回传 credential_type/username，但仅改 hostname 时审计应记为未变更。"""
    await _grant_cmdb_permissions(db_session, test_user)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-audit-01",
            "ip_address": "10.0.9.11",
            "vendor": "generic",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password": "orig-pwd",
        },
        headers=auth_headers,
    )
    asset_id = create_resp.json()["data"]["id"]

    update_resp = await client.patch(
        f"/api/v1/cmdb/assets/{asset_id}",
        json={
            "hostname": "srv-audit-01-renamed",
            "credential_type": "static",
            "credential_username": "admin",
        },
        headers=auth_headers,
    )
    assert update_resp.status_code == 200, update_resp.text

    detail = await _latest_update_audit_detail(db_session, asset_id)
    assert "凭据未变更" in detail
    assert "凭据已变更" not in detail


async def test_update_credential_change_audit_reports_changed(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实际修改凭据（用户名或密码）时，审计应记为已变更。"""
    await _grant_cmdb_permissions(db_session, test_user)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-audit-02",
            "ip_address": "10.0.9.12",
            "vendor": "generic",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password": "orig-pwd",
        },
        headers=auth_headers,
    )
    asset_id = create_resp.json()["data"]["id"]

    update_resp = await client.patch(
        f"/api/v1/cmdb/assets/{asset_id}",
        json={
            "credential_type": "static",
            "credential_username": "root",
            "credential_password": "new-pwd",
        },
        headers=auth_headers,
    )
    assert update_resp.status_code == 200, update_resp.text

    detail = await _latest_update_audit_detail(db_session, asset_id)
    assert "凭据已变更" in detail
    assert "new-pwd" not in detail


async def test_switch_to_static_without_password_is_rejected_when_no_existing_secret(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-api-04",
            "ip_address": "10.0.9.4",
            "vendor": "generic",
        },
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
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-api-05",
            "ip_address": "10.0.9.5",
            "vendor": "generic",
        },
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
        json={
            "asset_type": "server",
            "hostname": "srv-forbidden",
            "ip_address": "10.0.9.9",
            "vendor": "generic",
        },
        headers=auth_headers,
    )
    assert response.status_code == 403


async def _make_asset_pair(db_session: AsyncSession) -> tuple[int, int]:
    """直接落库创建一对资产，供依赖关系接口测试使用（不必经过创建资产 API）。"""
    from app.crud.cmdb_asset import cmdb_asset_crud

    parent = await cmdb_asset_crud.create(
        db_session,
        {"asset_type": "switch", "hostname": "sw-dep-01", "ip_address": "10.0.9.20", "vendor": "generic"},
    )
    child = await cmdb_asset_crud.create(
        db_session,
        {"asset_type": "server", "hostname": "srv-dep-01", "ip_address": "10.0.9.21", "vendor": "generic"},
    )
    await db_session.commit()
    return parent.id, child.id


async def test_create_dependency_then_list_shows_both_directions(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    parent_id, child_id = await _make_asset_pair(db_session)

    create_resp = await client.post(
        f"/api/v1/cmdb/assets/{parent_id}/dependencies",
        json={"child_asset_id": child_id, "relation_type": "uplink"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    body = create_resp.json()["data"]
    assert body == {
        "parent_asset_id": parent_id,
        "child_asset_id": child_id,
        "relation_type": "uplink",
    } | {"created_at": body["created_at"]}

    parent_list = await client.get(f"/api/v1/cmdb/assets/{parent_id}/dependencies", headers=auth_headers)
    assert parent_list.status_code == 200
    assert [c["child_asset_id"] for c in parent_list.json()["data"]["children"]] == [child_id]
    assert parent_list.json()["data"]["parents"] == []

    child_list = await client.get(f"/api/v1/cmdb/assets/{child_id}/dependencies", headers=auth_headers)
    assert child_list.status_code == 200
    assert [p["parent_asset_id"] for p in child_list.json()["data"]["parents"]] == [parent_id]
    assert child_list.json()["data"]["children"] == []


async def test_create_dependency_rejects_self_loop(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    parent_id, _child_id = await _make_asset_pair(db_session)

    response = await client.post(
        f"/api/v1/cmdb/assets/{parent_id}/dependencies",
        json={"child_asset_id": parent_id, "relation_type": "uplink"},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_create_dependency_rejects_unknown_child_asset(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    parent_id, _child_id = await _make_asset_pair(db_session)

    response = await client.post(
        f"/api/v1/cmdb/assets/{parent_id}/dependencies",
        json={"child_asset_id": 999999, "relation_type": "uplink"},
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_create_duplicate_dependency_returns_409(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    parent_id, child_id = await _make_asset_pair(db_session)
    payload = {"child_asset_id": child_id, "relation_type": "uplink"}

    first = await client.post(
        f"/api/v1/cmdb/assets/{parent_id}/dependencies", json=payload, headers=auth_headers
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/api/v1/cmdb/assets/{parent_id}/dependencies", json=payload, headers=auth_headers
    )
    assert second.status_code == 409


async def test_delete_dependency_removes_edge(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    parent_id, child_id = await _make_asset_pair(db_session)
    await client.post(
        f"/api/v1/cmdb/assets/{parent_id}/dependencies",
        json={"child_asset_id": child_id, "relation_type": "uplink"},
        headers=auth_headers,
    )

    delete_resp = await client.delete(
        f"/api/v1/cmdb/assets/{parent_id}/dependencies/{child_id}", headers=auth_headers
    )
    assert delete_resp.status_code == 200, delete_resp.text

    delete_again = await client.delete(
        f"/api/v1/cmdb/assets/{parent_id}/dependencies/{child_id}", headers=auth_headers
    )
    assert delete_again.status_code == 404


async def test_dependency_endpoints_require_permission(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    # test_user 默认没有任何 cmdb 权限（未调用 _grant_cmdb_permissions）
    parent_id, child_id = await _make_asset_pair(db_session)

    create_resp = await client.post(
        f"/api/v1/cmdb/assets/{parent_id}/dependencies",
        json={"child_asset_id": child_id, "relation_type": "uplink"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 403

    list_resp = await client.get(f"/api/v1/cmdb/assets/{parent_id}/dependencies", headers=auth_headers)
    assert list_resp.status_code == 403


async def _grant_cmdb_credential_read(db_session: AsyncSession, test_user) -> None:
    """为 test_user 追加 cmdb:credential_read 权限。"""
    from app.models.permission import Permission
    from app.models.role import role_permissions
    from app.models.user import user_roles

    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    permission = Permission(
        name="查看 CMDB 静态凭据",
        code="cmdb:credential_read",
        module="CMDB",
    )
    db_session.add(permission)
    await db_session.flush()
    await db_session.execute(
        role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
    )
    await db_session.commit()


async def _create_static_asset(
    client: AsyncClient,
    auth_headers: Headers,
    *,
    secret: str = "Sup3rSecretDevicePwd!",
    hostname: str = "srv-cred-01",
) -> int:
    """通过 API 创建带静态凭据的资产，返回 asset_id。"""
    response = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": hostname,
            "ip_address": "10.0.9.31",
            "vendor": "generic",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password": secret,
        },
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    asset_id: int = response.json()["data"]["id"]
    return asset_id


async def test_reveal_credential_requires_credential_read_permission(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅有 cmdb:read 时查看静态密码必须 403。"""
    await _grant_cmdb_permissions(db_session, test_user)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    asset_id = await _create_static_asset(client, auth_headers)

    response = await client.get(f"/api/v1/cmdb/assets/{asset_id}/credential", headers=auth_headers)
    assert response.status_code == 403


async def test_reveal_static_credential_returns_plaintext(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有 cmdb:credential_read 时可解密并返回加密前的明文。"""
    await _grant_cmdb_permissions(db_session, test_user)
    await _grant_cmdb_credential_read(db_session, test_user)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    secret = "RevealMeNow!123"
    asset_id = await _create_static_asset(client, auth_headers, secret=secret)

    response = await client.get(f"/api/v1/cmdb/assets/{asset_id}/credential", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["password"] == secret


async def test_reveal_credential_writes_audit_without_password(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查看密码后审计记录 action=view_cmdb_credential，detail 不含明文。"""
    await _grant_cmdb_permissions(db_session, test_user)
    await _grant_cmdb_credential_read(db_session, test_user)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    secret = "AuditSafePwd!456"
    asset_id = await _create_static_asset(client, auth_headers, secret=secret, hostname="srv-audit-cred")

    response = await client.get(f"/api/v1/cmdb/assets/{asset_id}/credential", headers=auth_headers)
    assert response.status_code == 200, response.text

    db_session.expire_all()
    audit_row = (
        await db_session.execute(
            select(AuditLog)
            .where(
                AuditLog.action == "view_cmdb_credential",
                AuditLog.target == f"cmdb_asset:{asset_id}",
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert "srv-audit-cred" in audit_row.detail
    assert secret not in audit_row.detail


async def test_asset_detail_still_has_no_password_field(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查看凭据后，资产详情接口仍不返回 password 字段。"""
    await _grant_cmdb_permissions(db_session, test_user)
    await _grant_cmdb_credential_read(db_session, test_user)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    asset_id = await _create_static_asset(client, auth_headers)

    await client.get(f"/api/v1/cmdb/assets/{asset_id}/credential", headers=auth_headers)
    detail_resp = await client.get(f"/api/v1/cmdb/assets/{asset_id}", headers=auth_headers)
    assert detail_resp.status_code == 200, detail_resp.text
    body = detail_resp.json()["data"]
    assert "password" not in body
    assert "credential_password" not in body
    assert "credential_password_encrypted" not in body
    assert body["credential_password_set"] is True


async def test_reveal_credential_rejects_non_static_asset(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    """credential_type=none 的资产查看密码应返回 422。"""
    await _grant_cmdb_permissions(db_session, test_user)
    await _grant_cmdb_credential_read(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-no-cred",
            "ip_address": "10.0.9.32",
            "vendor": "generic",
        },
        headers=auth_headers,
    )
    asset_id = create_resp.json()["data"]["id"]

    response = await client.get(f"/api/v1/cmdb/assets/{asset_id}/credential", headers=auth_headers)
    assert response.status_code == 422, response.text
    assert "该资产没有可查看的静态密码" in response.text
