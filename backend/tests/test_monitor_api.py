"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_monitor_api.py
@DateTime: 2026-08-13 14:00
@Docs: 监控目标管理 API：CRUD、查重、最近探测状态与权限
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.models.permission import Permission
from app.models.user import user_roles

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _grant_monitor_permissions(db_session: AsyncSession, test_user) -> None:
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


async def _grant_monitor_log_read(db_session: AsyncSession, test_user) -> None:
    """现场创建 monitor_log:read 并挂到 test_user 已有角色上。"""
    from sqlalchemy import select

    from app.models.role import role_permissions

    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    permission = Permission(name="查看监控日志", code="monitor_log:read", module="监控")
    db_session.add(permission)
    await db_session.flush()
    await db_session.execute(
        role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
    )
    await db_session.commit()


async def test_create_list_get_patch_delete_target(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
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
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
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
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    await _grant_monitor_permissions(db_session, test_user)
    payload = {"ip_address": "10.0.0.5", "port": 22, "label": "SSH"}
    first = await client.post("/api/v1/monitor/targets", json=payload, headers=auth_headers)
    assert first.status_code == 201, first.text

    second = await client.post("/api/v1/monitor/targets", json=payload, headers=auth_headers)
    assert second.status_code == 409


async def test_invalid_cmdb_asset_id_returns_422(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    await _grant_monitor_permissions(db_session, test_user)
    response = await client.post(
        "/api/v1/monitor/targets",
        json={"ip_address": "10.0.0.5", "port": 22, "cmdb_asset_id": 99999},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_create_with_existing_cmdb_asset(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
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
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    await _grant_monitor_permissions(db_session, test_user)
    response = await client.get("/api/v1/monitor/runtime", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["sweep_interval_seconds"] >= 5


async def test_monitor_logs_requires_monitor_log_read_permission(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    """仅有 monitor:read 时访问监控日志必须 403。"""
    await _grant_monitor_permissions(db_session, test_user)
    response = await client.get("/api/v1/monitor/logs", headers=auth_headers)
    assert response.status_code == 403


async def test_monitor_logs_filters_by_target_id_and_status(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    """有 monitor_log:read 时可按 target_id 与 status 筛到 down 行。"""
    await _grant_monitor_log_read(db_session, test_user)
    target_a = await monitor_target_crud.create(
        db_session,
        {"ip_address": "10.0.0.11", "port": 22, "label": "SSH-A", "is_active": True},
    )
    target_b = await monitor_target_crud.create(
        db_session,
        {"ip_address": "10.0.0.12", "port": 443, "label": "HTTPS-B", "is_active": True},
    )
    await monitor_status_event_crud.record(
        db_session, target_id=target_a.id, status="down", latency_ms=None, detail="连接超时"
    )
    await monitor_status_event_crud.record(
        db_session, target_id=target_a.id, status="up", latency_ms=8, detail=""
    )
    await monitor_status_event_crud.record(
        db_session, target_id=target_b.id, status="down", latency_ms=None, detail="拒绝连接"
    )
    await db_session.commit()

    response = await client.get(
        f"/api/v1/monitor/logs?target_id={target_a.id}&status=down",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["target_id"] == target_a.id
    assert item["status"] == "down"
    assert item["detail"] == "连接超时"
    assert item["label"] == "SSH-A"
    assert item["ip_address"] == "10.0.0.11"
    assert item["port"] == 22


# ---------------------------------------------------------------------------
# 可用率状态条：列表接口顺带返回整条图表的数据，前端一次请求渲染完成。
#
# monitor_status_events 存的是**状态变化**（record_probe 同状态就地更新
# checked_at、变状态才追加行），不是探测流水。所以一个持续在线一小时的目标
# 在表里只有 1 行——接口必须把这 1 行还原成整条绿色，而不是只点亮最后一格。


async def _backdate(
    db_session: AsyncSession, target_id: int, *, created_minutes_ago: int
) -> None:
    """把目标创建时间往前挪，模拟一个已经存在一段时间的目标。

    状态条不会画目标存在之前的时间（那时它还没建，画任何颜色都是编的），
    所以测「整条全绿」必须让目标比窗口更早存在。
    """
    target = await monitor_target_crud.get(db_session, target_id)
    assert target is not None
    target.created_at = datetime.now(UTC) - timedelta(minutes=created_minutes_ago)
    await db_session.flush()


async def test_single_up_row_paints_the_whole_strip_green(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    """**回归测试：之前这里只有最后一格是绿的，其余全灰。**

    一个持续在线的目标只有 1 行状态变化记录。把它当成孤立的探测点去分桶，
    就只有 checked_at 所在的那一格有颜色——整条图等于废掉。
    正确做法是把它还原成阶跃：这一行代表「一直是 up，最后确认于 checked_at」。
    """
    await _grant_monitor_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/monitor/targets",
        json={"ip_address": "10.0.0.31", "port": 80, "label": "HTTP"},
        headers=auth_headers,
    )
    target_id = create_resp.json()["data"]["id"]
    await _backdate(db_session, target_id, created_minutes_ago=180)

    await monitor_status_event_crud.record(db_session, target_id=target_id, status="up")
    await db_session.commit()

    item = (
        await client.get("/api/v1/monitor/targets", headers=auth_headers)
    ).json()["data"]["items"][0]

    window = item["uptime_window"]
    assert len(window["buckets"]) == 60
    assert window["bucket_seconds"] == 60
    # 关键断言：整条都得是绿的，不是只有最后一格
    assert set(window["buckets"]) == {"up"}
    assert window["uptime_rate"] == 1.0


async def test_target_without_probes_reports_unknown_not_full_uptime(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    """从没探测过的目标不能显示「100% 可用」——那是撒谎，格子也该是灰的。"""
    await _grant_monitor_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/monitor/targets",
        json={"ip_address": "10.0.0.32", "port": 80, "label": "新建"},
        headers=auth_headers,
    )
    await _backdate(db_session, create_resp.json()["data"]["id"], created_minutes_ago=180)
    await db_session.commit()

    item = (
        await client.get("/api/v1/monitor/targets", headers=auth_headers)
    ).json()["data"]["items"][0]

    window = item["uptime_window"]
    assert window["uptime_rate"] is None
    assert set(window["buckets"]) == {"unknown"}


async def test_strip_shows_the_outage_before_recovery(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    """故障恢复后，故障那段仍要留在条上——否则回看历史没有意义。"""
    await _grant_monitor_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/monitor/targets",
        json={"ip_address": "10.0.0.34", "port": 80, "label": "抖过一次"},
        headers=auth_headers,
    )
    target_id = create_resp.json()["data"]["id"]
    await _backdate(db_session, target_id, created_minutes_ago=180)

    now = datetime.now(UTC)
    down = await monitor_status_event_crud.record(
        db_session, target_id=target_id, status="down"
    )
    down.checked_at = now - timedelta(minutes=40)
    up = await monitor_status_event_crud.record(
        db_session, target_id=target_id, status="up"
    )
    up.checked_at = now
    await db_session.commit()

    item = (
        await client.get("/api/v1/monitor/targets", headers=auth_headers)
    ).json()["data"]["items"][0]

    buckets = item["uptime_window"]["buckets"]
    assert "down" in buckets, "故障段必须留在条上"
    assert buckets[-1] == "up", "已恢复，最后一格该是绿的"


async def test_single_target_detail_also_carries_the_window(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    """详情接口与列表用同一个响应模型，字段不能只在列表里有值。"""
    await _grant_monitor_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/monitor/targets",
        json={"ip_address": "10.0.0.33", "port": 80, "label": "详情"},
        headers=auth_headers,
    )
    target_id = create_resp.json()["data"]["id"]
    await _backdate(db_session, target_id, created_minutes_ago=180)
    await monitor_status_event_crud.record(db_session, target_id=target_id, status="up")
    await db_session.commit()

    detail = (
        await client.get(f"/api/v1/monitor/targets/{target_id}", headers=auth_headers)
    ).json()["data"]

    assert detail["uptime_window"]["uptime_rate"] == 1.0
    assert set(detail["uptime_window"]["buckets"]) == {"up"}
