"""设备命令策略管理 API：CRUD、回收站、权限门控与审计。"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _grant_policy_permissions(
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    *,
    read: bool = True,
    manage: bool = True,
) -> None:
    """现场创建 device_command_policy 权限并挂到 test_user 角色上。"""
    from app.models.permission import Permission
    from app.models.role import role_permissions
    from app.models.user import user_roles

    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    grants: list[tuple[str, str]] = []
    if read:
        grants.append(("device_command_policy:read", "查看设备命令策略"))
    if manage:
        grants.append(("device_command_policy:manage", "管理设备命令策略"))
    for code, name in grants:
        permission = Permission(name=name, code=code, module="设备命令策略")
        db_session.add(permission)
        await db_session.flush()
        await db_session.execute(
            role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
        )
    await db_session.commit()


async def test_create_asset_type_policy_success(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
) -> None:
    await _grant_policy_permissions(db_session, test_user)

    response = await client.post(
        "/api/v1/device-command-policies/policies",
        json={
            "scope": "asset_type",
            "asset_type": "server",
            "command_name": "show_version",
            "decision": "whitelist",
            "note": "允许查看版本",
        },
        headers=auth_headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["scope"] == "asset_type"
    assert body["asset_type"] == "server"
    assert body["asset_id"] is None
    assert body["command_name"] == "show_version"
    assert body["decision"] == "whitelist"
    assert body["created_by_user_id"] == test_user.id


async def test_create_policy_rejects_asset_type_scope_for_state_changing_command(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
) -> None:
    await _grant_policy_permissions(db_session, test_user)
    response = await client.post(
        "/api/v1/device-command-policies/policies",
        json={
            "scope": "asset_type",
            "asset_type": "switch",
            "command_name": "reboot",
            "decision": "whitelist",
        },
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_create_unknown_command_name_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
) -> None:
    await _grant_policy_permissions(db_session, test_user)

    response = await client.post(
        "/api/v1/device-command-policies/policies",
        json={
            "scope": "asset_type",
            "asset_type": "server",
            "command_name": "not_in_catalog",
            "decision": "whitelist",
        },
        headers=auth_headers,
    )

    assert response.status_code == 422, response.text


async def test_create_duplicate_policy_returns_409(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
) -> None:
    await _grant_policy_permissions(db_session, test_user)
    payload = {
        "scope": "asset_type",
        "asset_type": "server",
        "command_name": "ping",
        "decision": "whitelist",
    }

    first = await client.post(
        "/api/v1/device-command-policies/policies",
        json=payload,
        headers=auth_headers,
    )
    assert first.status_code == 201, first.text

    duplicate = await client.post(
        "/api/v1/device-command-policies/policies",
        json={**payload, "decision": "blacklist"},
        headers=auth_headers,
    )
    assert duplicate.status_code == 409, duplicate.text


async def test_read_only_permission_cannot_create(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
) -> None:
    await _grant_policy_permissions(db_session, test_user, manage=False)

    response = await client.post(
        "/api/v1/device-command-policies/policies",
        json={
            "scope": "asset_type",
            "asset_type": "server",
            "command_name": "show_version",
            "decision": "whitelist",
        },
        headers=auth_headers,
    )

    assert response.status_code == 403, response.text


async def test_soft_delete_restore_purge_flow(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
) -> None:
    await _grant_policy_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/device-command-policies/policies",
        json={
            "scope": "asset_type",
            "asset_type": "switch",
            "command_name": "show_interfaces",
            "decision": "blacklist",
        },
        headers=auth_headers,
    )
    policy_id = create_resp.json()["data"]["id"]

    delete_resp = await client.delete(
        f"/api/v1/device-command-policies/policies/{policy_id}",
        headers=auth_headers,
    )
    assert delete_resp.status_code == 200, delete_resp.text

    get_resp = await client.get(
        f"/api/v1/device-command-policies/policies/{policy_id}",
        headers=auth_headers,
    )
    assert get_resp.status_code == 404

    deleted_resp = await client.get(
        "/api/v1/device-command-policies/policies/deleted",
        headers=auth_headers,
    )
    assert deleted_resp.status_code == 200
    assert any(item["id"] == policy_id for item in deleted_resp.json()["data"]["items"])

    restore_resp = await client.post(
        f"/api/v1/device-command-policies/policies/{policy_id}/restore",
        headers=auth_headers,
    )
    assert restore_resp.status_code == 200, restore_resp.text

    delete_again = await client.delete(
        f"/api/v1/device-command-policies/policies/{policy_id}",
        headers=auth_headers,
    )
    assert delete_again.status_code == 200
    purge_resp = await client.delete(
        f"/api/v1/device-command-policies/policies/{policy_id}/purge",
        headers=auth_headers,
    )
    assert purge_resp.status_code == 200, purge_resp.text

    purge_again = await client.delete(
        f"/api/v1/device-command-policies/policies/{policy_id}/purge",
        headers=auth_headers,
    )
    assert purge_again.status_code == 404


async def test_restore_conflict_returns_409(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
) -> None:
    """软删后新建同目标策略，再恢复旧策略应 409，不能制造重复活跃行。"""
    await _grant_policy_permissions(db_session, test_user)
    first = await client.post(
        "/api/v1/device-command-policies/policies",
        json={
            "scope": "asset_type",
            "asset_type": "firewall",
            "command_name": "show_version",
            "decision": "whitelist",
        },
        headers=auth_headers,
    )
    assert first.status_code == 201, first.text
    first_id = first.json()["data"]["id"]

    delete_resp = await client.delete(
        f"/api/v1/device-command-policies/policies/{first_id}",
        headers=auth_headers,
    )
    assert delete_resp.status_code == 200, delete_resp.text

    second = await client.post(
        "/api/v1/device-command-policies/policies",
        json={
            "scope": "asset_type",
            "asset_type": "firewall",
            "command_name": "show_version",
            "decision": "blacklist",
        },
        headers=auth_headers,
    )
    assert second.status_code == 201, second.text

    restore_resp = await client.post(
        f"/api/v1/device-command-policies/policies/{first_id}/restore",
        headers=auth_headers,
    )
    assert restore_resp.status_code == 409, restore_resp.text


async def test_create_and_update_write_audit(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
) -> None:
    await _grant_policy_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/device-command-policies/policies",
        json={
            "scope": "asset_type",
            "asset_type": "router",
            "command_name": "show_running_config",
            "decision": "blacklist",
            "note": "初始备注",
        },
        headers=auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    policy_id = create_resp.json()["data"]["id"]

    db_session.expire_all()
    create_audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "create_device_command_policy",
                AuditLog.target == f"device_command_policy:{policy_id}",
            )
        )
    ).scalar_one_or_none()
    assert create_audit is not None

    update_resp = await client.patch(
        f"/api/v1/device-command-policies/policies/{policy_id}",
        json={"decision": "whitelist", "note": "已放宽"},
        headers=auth_headers,
    )
    assert update_resp.status_code == 200, update_resp.text

    db_session.expire_all()
    update_audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "update_device_command_policy",
                AuditLog.target == f"device_command_policy:{policy_id}",
            )
        )
    ).scalar_one_or_none()
    assert update_audit is not None
