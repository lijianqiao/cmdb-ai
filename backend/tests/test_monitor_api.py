"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_monitor_api.py
@DateTime: 2026-08-13 14:00
@Docs: 监控目标管理 API：CRUD、查重、最近探测状态与权限
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.monitor_status_event import monitor_status_event_crud
from app.models.permission import Permission
from app.models.user import user_roles

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _grant_monitor_permissions(db_session: AsyncSession, test_user) -> None:  # noqa: ANN001
    """现场创建 monitor:read + monitor:manage 并挂到 test_user 已有角色上。"""
    from sqlalchemy import select

    from app.models.role import role_permissions

    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    for code, name in (("monitor:read", "查看监控目标与状态"), ("monitor:manage", "管理监控目标")):
        permission = Permission(name=name, code=code, module="监控")
        db_session.add(permission)
        await db_session.flush()
        await db_session.execute(
            role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
        )
    await db_session.commit()


async def test_create_list_get_patch_delete_target(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_monitor_permissions(db_session, test_user)

    create_resp = await client.post(
        "/api/v1/monitor/targets",
        json={
            "ip_address": "10.0.0.5",
            "port": 22,
            "label": "核心 SSH",
            "check_interval_seconds": 30,
            "is_active": True,
        },
        headers=auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()["data"]
    target_id = created["id"]
    assert created["ip_address"] == "10.0.0.5"
    assert created["latest_status"] is None

    list_resp = await client.get("/api/v1/monitor/targets", headers=auth_headers)
    assert list_resp.status_code == 200, list_resp.text
    assert list_resp.json()["data"]["total"] == 1
    assert list_resp.json()["data"]["items"][0]["id"] == target_id

    get_resp = await client.get(f"/api/v1/monitor/targets/{target_id}", headers=auth_headers)
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["data"]["label"] == "核心 SSH"

    patch_resp = await client.patch(
        f"/api/v1/monitor/targets/{target_id}",
        json={"is_active": False, "label": "已停用 SSH"},
        headers=auth_headers,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["data"]["is_active"] is False
    assert patch_resp.json()["data"]["label"] == "已停用 SSH"

    delete_resp = await client.delete(f"/api/v1/monitor/targets/{target_id}", headers=auth_headers)
    assert delete_resp.status_code == 200, delete_resp.text

    missing = await client.get(f"/api/v1/monitor/targets/{target_id}", headers=auth_headers)
    assert missing.status_code == 404


async def test_list_includes_latest_probe_status(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_monitor_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/monitor/targets",
        json={"ip_address": "10.0.0.8", "port": 443, "label": "HTTPS"},
        headers=auth_headers,
    )
    target_id = create_resp.json()["data"]["id"]

    await monitor_status_event_crud.record(
        db_session, target_id=target_id, status="up", latency_ms=12, detail=""
    )
    await db_session.commit()

    list_resp = await client.get("/api/v1/monitor/targets", headers=auth_headers)
    item = list_resp.json()["data"]["items"][0]
    assert item["latest_status"] == "up"
    assert item["latest_latency_ms"] == 12


async def test_duplicate_ip_port_returns_409(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_monitor_permissions(db_session, test_user)
    payload = {"ip_address": "10.0.0.5", "port": 22, "label": "SSH"}
    first = await client.post("/api/v1/monitor/targets", json=payload, headers=auth_headers)
    assert first.status_code == 201, first.text

    second = await client.post("/api/v1/monitor/targets", json=payload, headers=auth_headers)
    assert second.status_code == 409


async def test_invalid_cmdb_asset_id_returns_422(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_monitor_permissions(db_session, test_user)
    response = await client.post(
        "/api/v1/monitor/targets",
        json={"ip_address": "10.0.0.5", "port": 22, "cmdb_asset_id": 99999},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_create_with_existing_cmdb_asset(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_monitor_permissions(db_session, test_user)
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "server",
            "hostname": "srv-mon-01",
            "ip_address": "10.0.0.5",
            "vendor": "generic",
        },
    )
    await db_session.commit()

    response = await client.post(
        "/api/v1/monitor/targets",
        json={
            "ip_address": "10.0.0.5",
            "port": 22,
            "cmdb_asset_id": asset.id,
        },
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["cmdb_asset_id"] == asset.id


async def test_monitor_endpoints_require_permission(
    client: AsyncClient, auth_headers: Headers
) -> None:
    list_resp = await client.get("/api/v1/monitor/targets", headers=auth_headers)
    assert list_resp.status_code == 403

    runtime_resp = await client.get("/api/v1/monitor/runtime", headers=auth_headers)
    assert runtime_resp.status_code == 403

    create_resp = await client.post(
        "/api/v1/monitor/targets",
        json={"ip_address": "10.0.0.5", "port": 22},
        headers=auth_headers,
    )
    assert create_resp.status_code == 403


async def test_monitor_runtime_returns_sweep_interval(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_monitor_permissions(db_session, test_user)
    response = await client.get("/api/v1/monitor/runtime", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["sweep_interval_seconds"] >= 5
