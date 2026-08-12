"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_ws_api.py
@DateTime: 2026-08-12 12:31
@Docs: 验证 Agent WebSocket 路由的 JWT 鉴权、会话归属与 Hub 广播。
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from app.agent.ws_hub import hub
from app.core.security import hash_password
from app.crud.agent_session import agent_session_crud
from app.main import app
from app.models.role import Role
from app.models.user import User
from app.schemas.agent_ws import AgentWsServerMessage

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


@pytest_asyncio.fixture(autouse=True)
async def _clear_ws_hub() -> AsyncIterator[None]:
    """每个用例前后清空进程内 Hub，避免跨测串扰。"""
    hub._connections.clear()
    yield
    hub._connections.clear()


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
