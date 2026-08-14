"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_ws_api.py
@DateTime: 2026-08-12 12:31
@Docs: 验证 Agent WebSocket 路由的 JWT 鉴权、会话归属与 Hub 广播。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from app.agent.ws_hub import hub
from app.core.security import hash_password
from app.crud.agent_session import agent_session_crud
from app.main import app
from app.models.permission import Permission
from app.models.role import Role, role_permissions
from app.models.user import User, user_roles
from app.schemas.agent_ws import AgentWsServerMessage

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


@pytest_asyncio.fixture(autouse=True)
async def _clear_ws_hub() -> AsyncIterator[None]:
    """每个用例前后清空进程内 Hub，避免跨测串扰。"""
    for peers in hub._connections.values():
        for peer in peers.values():
            peer.writer_task.cancel()
    hub._connections.clear()
    yield
    for peers in list(hub._connections.values()):
        for peer in peers.values():
            peer.writer_task.cancel()
            with suppress(asyncio.CancelledError):
                await peer.writer_task
    hub._connections.clear()


@pytest_asyncio.fixture(autouse=True)
async def _grant_agent_use(db_session: AsyncSession, test_role: Role) -> None:
    """自动给 test_role 挂上 agent:use——运维助手会话入口现在需要这个权限，
    测试库的权限种子（conftest.py::test_permissions）不含 Agent 模块，需要
    本文件自己现场创建，跟 test_hitl_api.py::_grant_hitl_approve 是同一模式。
    """
    permission = Permission(name="使用运维助手", code="agent:use", module="Agent")
    db_session.add(permission)
    await db_session.flush()
    await db_session.execute(
        role_permissions.insert().values(role_id=test_role.id, permission_id=permission.id)
    )
    await db_session.commit()


def _access_token_from_headers(headers: Headers) -> str:
    """从 Authorization Bearer 头取出 JWT。"""
    auth = headers["Authorization"]
    assert auth.startswith("Bearer ")
    return auth.removeprefix("Bearer ")


def _ws_client() -> TestClient:
    """构造测试用 TestClient（不进入上下文，避免 lifespan 打全局库）。"""
    return TestClient(app, base_url="http://test", raise_server_exceptions=False)


# Starlette websocket_connect 默认 Host=testserver；覆盖为 test 以通过 TrustedHost
_WS_HEADERS = {"host": "test"}


def _connect_expect_close(path: str) -> tuple[int, str]:
    """连接 WebSocket 并期望服务端关闭，返回 (code, reason)。"""
    client = _ws_client()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(path, headers=_WS_HEADERS) as websocket:
            websocket.receive_text()
    disconnect = exc_info.value
    return disconnect.code, disconnect.reason


async def _wait_until_hub_has(session_id: int, *, max_wait_seconds: float = 1.0) -> None:
    """在应用事件循环上轮询，直到 Hub 注册了该会话连接。"""
    deadline = asyncio.get_running_loop().time() + max_wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        if session_id in hub._connections and hub._connections[session_id]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Hub 在 {max_wait_seconds}s 内未注册 session_id={session_id}")


async def test_ws_connect_without_monitor_read_has_no_monitor_capability(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """仅有 agent:use 的用户建连后，peer 不具备 monitor:read 能力。"""
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "无监控权限", "status": "active"},
    )
    await db_session.commit()
    token = _access_token_from_headers(auth_headers)
    path = f"/api/v1/ws/agent/{session.id}?access_token={token}"

    ws_client = _ws_client()
    with ws_client.websocket_connect(path, headers=_WS_HEADERS) as websocket:
        websocket.portal.call(_wait_until_hub_has, session.id)
        peer = next(iter(hub._connections[session.id].values()))
        assert peer.can_read_monitor is False


async def test_ws_with_monitor_read_receives_monitor_alert(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    test_role: Role,
) -> None:
    """具备 monitor:read 的用户建连后，能收到全局 monitor_alert 广播。"""
    permission = Permission(name="查看监控", code="monitor:read", module="Monitor")
    db_session.add(permission)
    await db_session.flush()
    await db_session.execute(
        role_permissions.insert().values(role_id=test_role.id, permission_id=permission.id)
    )
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "有监控权限", "status": "active"},
    )
    await db_session.commit()
    token = _access_token_from_headers(auth_headers)
    path = f"/api/v1/ws/agent/{session.id}?access_token={token}"
    alert_message = AgentWsServerMessage(
        type="monitor_alert",
        payload={"target_id": 7, "status": "down", "message": "核心交换机离线"},
    )

    ws_client = _ws_client()
    with ws_client.websocket_connect(path, headers=_WS_HEADERS) as websocket:
        websocket.portal.call(_wait_until_hub_has, session.id)
        peer = next(iter(hub._connections[session.id].values()))
        assert peer.can_read_monitor is True
        websocket.portal.call(hub.broadcast_monitor_alert, alert_message)
        data = websocket.receive_json()

    assert data["type"] == "monitor_alert"
    assert data["payload"] == {"target_id": 7, "status": "down", "message": "核心交换机离线"}


async def test_periodic_reauth_refreshes_monitor_access_without_closing_chat(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    test_role: Role,
) -> None:
    """连接期间撤销 monitor:read 时，下一轮复查只关闭告警能力，不关闭聊天连接。"""
    import app.api.v1.agent_ws as agent_ws_module
    from app.core.security import decode_token

    permission = Permission(name="查看监控", code="monitor:read", module="Monitor")
    db_session.add(permission)
    await db_session.flush()
    await db_session.execute(
        role_permissions.insert().values(role_id=test_role.id, permission_id=permission.id)
    )
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "复查监控权限", "status": "active"},
    )
    await db_session.commit()
    token = _access_token_from_headers(auth_headers)
    payload = decode_token(token)

    close_calls: list[tuple[int, str]] = []

    async def fake_close(*, code: int, reason: str) -> None:
        close_calls.append((code, reason))

    fake_websocket = AsyncMock()
    fake_websocket.app = app
    fake_websocket.close = fake_close

    await hub.connect(session.id, fake_websocket, can_read_monitor=True)

    reauth_task = asyncio.create_task(
        agent_ws_module._periodic_reauth(
            fake_websocket,
            user_id=test_user.id,
            family_id=payload.sid,
            token_version=payload.ver,
            session_id=session.id,
            interval_seconds=0.02,
        )
    )
    try:
        role_id = (
            await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
        ).scalar_one()
        permission_id = (
            await db_session.execute(select(Permission.id).where(Permission.code == "monitor:read"))
        ).scalar_one()
        await db_session.execute(
            delete(role_permissions).where(
                role_permissions.c.role_id == role_id,
                role_permissions.c.permission_id == permission_id,
            )
        )
        await db_session.commit()

        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            peer = hub._connections.get(session.id, {}).get(fake_websocket)
            if peer is not None and peer.can_read_monitor is False:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("monitor:read 撤销后 peer 能力未刷新为 False")
    finally:
        if not reauth_task.done():
            reauth_task.cancel()
            with suppress(asyncio.CancelledError):
                await reauth_task

    assert close_calls == []
    assert fake_websocket in hub._connections.get(session.id, {})
    peer = hub._connections[session.id][fake_websocket]
    assert peer.can_read_monitor is False
    await hub.disconnect(session.id, fake_websocket)


async def test_ws_without_token_closes_4401(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 access_token 且不发首帧时，超时后应关闭，码 4401。"""
    import app.api.v1.agent_ws as agent_ws_module

    # 缩短首帧等待，避免单测空等 5 秒
    monkeypatch.setattr(agent_ws_module, "_AUTH_FRAME_TIMEOUT_SECONDS", 0.2)
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "无 token", "status": "active"},
    )
    await db_session.commit()

    code, reason = _connect_expect_close(f"/api/v1/ws/agent/{session.id}")
    assert code == 4401
    assert "认证" in reason or "token" in reason.casefold()


async def test_ws_invalid_token_closes_4401(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """错误 JWT 应关闭，码 4401。"""
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "错 token", "status": "active"},
    )
    await db_session.commit()

    code, reason = _connect_expect_close(
        f"/api/v1/ws/agent/{session.id}?access_token=not-a-valid-jwt"
    )
    assert code == 4401
    assert reason


async def test_ws_non_owner_closes_4403(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    test_role: Role,
) -> None:
    """非会话所有者连接应关闭，码 4403。"""
    other = User(
        username="otherws",
        email="otherws@example.com",
        hashed_password=hash_password("otherpassword123"),
        nickname="他人",
        is_active=True,
        is_superuser=False,
        roles=[test_role],
    )
    db_session.add(other)
    await db_session.flush()
    foreign_session = await agent_session_crud.create(
        db_session,
        {"user_id": other.id, "title": "他人会话", "status": "active"},
    )
    await db_session.commit()

    token = _access_token_from_headers(auth_headers)
    code, reason = _connect_expect_close(
        f"/api/v1/ws/agent/{foreign_session.id}?access_token={token}"
    )
    assert code == 4403
    assert reason


async def test_ws_missing_session_closes_4404(
    client: AsyncClient,
    auth_headers: Headers,
) -> None:
    """会话不存在应关闭，码 4404。"""
    token = _access_token_from_headers(auth_headers)
    code, reason = _connect_expect_close(
        f"/api/v1/ws/agent/999999?access_token={token}"
    )
    assert code == 4404
    assert reason


async def test_ws_inactive_session_closes_4403(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """非 active 会话即使归属正确也应拒绝（4403）。"""
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "已关闭", "status": "closed"},
    )
    await db_session.commit()
    token = _access_token_from_headers(auth_headers)
    code, reason = _connect_expect_close(
        f"/api/v1/ws/agent/{session.id}?access_token={token}"
    )
    assert code == 4403
    assert reason


async def test_ws_query_token_connect_receives_broadcast(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """默认 query token 鉴权成功后应注册到 Hub，并能收到 broadcast。"""
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "广播", "status": "active"},
    )
    await db_session.commit()
    token = _access_token_from_headers(auth_headers)
    path = f"/api/v1/ws/agent/{session.id}?access_token={token}"
    message = AgentWsServerMessage(
        type="assistant_delta",
        payload={"text": "你好", "done": False},
    )

    ws_client = _ws_client()
    with ws_client.websocket_connect(path, headers=_WS_HEADERS) as websocket:
        # accept 先于鉴权完成返回；需等到 hub.connect
        websocket.portal.call(_wait_until_hub_has, session.id)
        assert len(hub._connections[session.id]) == 1
        # 在 WebSocket 会话同一 portal 上调度 broadcast，避免跨事件循环
        websocket.portal.call(hub.broadcast, session.id, message)
        data = websocket.receive_json()

    assert data["type"] == "assistant_delta"
    assert data["payload"]["text"] == "你好"
    assert session.id not in hub._connections


async def test_ws_first_frame_auth_connects(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """无 query token 时，首帧 auth 应能完成鉴权并注册连接。"""
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "首帧", "status": "active"},
    )
    await db_session.commit()
    token = _access_token_from_headers(auth_headers)
    path = f"/api/v1/ws/agent/{session.id}"

    ws_client = _ws_client()
    with ws_client.websocket_connect(path, headers=_WS_HEADERS) as websocket:
        websocket.send_json({"type": "auth", "access_token": token})
        websocket.portal.call(_wait_until_hub_has, session.id)
        assert len(hub._connections[session.id]) == 1


async def test_ws_without_agent_use_permission_closes_4403(
    client: AsyncClient,
    db_session: AsyncSession,
    login_user,  # noqa: ANN001
) -> None:
    """没有 agent:use 权限的用户，即使会话就是自己的，也应该在握手阶段被拒绝。"""
    role = Role(name="无权限角色", description="", permissions=[])
    db_session.add(role)
    await db_session.flush()
    user = User(
        username="nopermws",
        email="nopermws@example.com",
        hashed_password=hash_password("nopermpassword123"),
        nickname="无权限用户",
        is_active=True,
        is_superuser=False,
        roles=[role],
    )
    db_session.add(user)
    await db_session.flush()
    session = await agent_session_crud.create(
        db_session, {"user_id": user.id, "title": "无权限", "status": "active"}
    )
    await db_session.commit()

    headers = await login_user("nopermws", "nopermpassword123")
    token = _access_token_from_headers(headers)

    code, reason = _connect_expect_close(f"/api/v1/ws/agent/{session.id}?access_token={token}")
    assert code == 4403
    assert reason


async def test_periodic_reauth_closes_socket_when_permission_revoked(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """连接建立后权限被收回，下一轮周期性复查应主动关闭连接。

    直接单测 _periodic_reauth 本身，不经过真实 WebSocket 收发栈 + TestClient
    的跨线程 portal——那条路径下测试自己的 DB 写入和服务端复查任务的 DB 读取
    会在同一个 StaticPool 单连接上产生真实的并发访问，导致收尾时序不稳定
    （偶发 "no active connection"，跟这里要验证的行为本身无关）。这里让
    revoke 和 _periodic_reauth 的 DB 访问都跑在测试自己的事件循环上，天然
    没有跨循环/跨线程的问题；连接建立阶段权限缺失会直接 4403 的分支已经由
    test_ws_without_agent_use_permission_closes_4403 做真实集成验证。
    """
    import app.api.v1.agent_ws as agent_ws_module
    from app.core.security import decode_token

    session = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "复查权限", "status": "active"}
    )
    await db_session.commit()
    token = _access_token_from_headers(auth_headers)
    payload = decode_token(token)

    close_calls: list[tuple[int, str]] = []

    async def fake_close(*, code: int, reason: str) -> None:
        close_calls.append((code, reason))

    fake_websocket = AsyncMock()
    fake_websocket.app = app
    fake_websocket.close = fake_close

    reauth_task = asyncio.create_task(
        agent_ws_module._periodic_reauth(
            fake_websocket,
            user_id=test_user.id,
            family_id=payload.sid,
            token_version=payload.ver,
            session_id=session.id,
            interval_seconds=0.02,
        )
    )
    try:
        role_id = (
            await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
        ).scalar_one()
        permission_id = (
            await db_session.execute(select(Permission.id).where(Permission.code == "agent:use"))
        ).scalar_one()
        await db_session.execute(
            delete(role_permissions).where(
                role_permissions.c.role_id == role_id,
                role_permissions.c.permission_id == permission_id,
            )
        )
        await db_session.commit()

        await asyncio.wait_for(reauth_task, timeout=2.0)
    finally:
        if not reauth_task.done():
            reauth_task.cancel()

    assert close_calls == [(4403, "权限已被收回")]
