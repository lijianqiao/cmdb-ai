"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_sessions_api.py
@DateTime: 2026-08-12 12:43
@Docs: 验证 Agent 会话 REST API：创建、列表、详情与根 transcript 历史。
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.crud.agent_message import agent_message_crud
from app.crud.agent_session import agent_session_crud
from app.models.permission import Permission
from app.models.role import Role, role_permissions
from app.models.user import User

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


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


async def _other_user(db: AsyncSession, role: Role) -> User:
    """创建另一个活跃用户，用于验证非所有者访问。"""
    user = User(
        username="other_session_user",
        email="other_session@example.com",
        hashed_password=hash_password("testpassword123"),
        nickname="其他用户",
        is_active=True,
        is_superuser=False,
        roles=[role],
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_create_session_requires_auth(client: AsyncClient) -> None:
    """未登录创建会话应返回 401。"""
    response = await client.post("/api/v1/agent/sessions", json={"title": "未登录"})
    assert response.status_code == 401


async def test_create_and_list_sessions(
    client: AsyncClient,
    auth_headers: Headers,
) -> None:
    """登录用户可创建会话，列表只返回自己的会话。"""
    create_resp = await client.post(
        "/api/v1/agent/sessions",
        json={"title": "网段巡检"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()["data"]
    assert created["title"] == "网段巡检"
    assert created["status"] == "active"
    assert created["approval_mode"] == "ask"
    assert "id" in created
    assert "user_id" in created

    empty_title_resp = await client.post(
        "/api/v1/agent/sessions",
        json={},
        headers=auth_headers,
    )
    assert empty_title_resp.status_code == 201, empty_title_resp.text
    assert empty_title_resp.json()["data"]["title"] == ""

    list_resp = await client.get("/api/v1/agent/sessions", headers=auth_headers)
    assert list_resp.status_code == 200, list_resp.text
    payload = list_resp.json()["data"]
    # 分页包络：items + total
    assert "items" in payload
    assert payload["total"] >= 2
    titles = {item["title"] for item in payload["items"]}
    assert "网段巡检" in titles
    assert "" in titles
    for item in payload["items"]:
        assert "approval_mode" in item
        assert item["approval_mode"] == "ask"


async def test_get_session_detail_and_non_owner_404(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    test_role: Role,
    auth_headers: Headers,
    login_user,
) -> None:
    """所有者可看详情；非所有者与不存在会话统一 404。"""
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "我的会话", "status": "active"},
    )
    await db_session.commit()

    own = await client.get(
        f"/api/v1/agent/sessions/{session.id}",
        headers=auth_headers,
    )
    assert own.status_code == 200, own.text
    assert own.json()["data"]["id"] == session.id
    assert own.json()["data"]["title"] == "我的会话"
    assert own.json()["data"]["approval_mode"] == "ask"

    missing = await client.get("/api/v1/agent/sessions/999999", headers=auth_headers)
    assert missing.status_code == 404

    other = await _other_user(db_session, test_role)
    other_headers = await login_user(other.username, "testpassword123")
    forbidden = await client.get(
        f"/api/v1/agent/sessions/{session.id}",
        headers=other_headers,
    )
    assert forbidden.status_code == 404


async def test_patch_approval_mode_owner_and_audit(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    create_resp = await client.post(
        "/api/v1/agent/sessions",
        json={"title": "改档"},
        headers=auth_headers,
    )
    session_id = create_resp.json()["data"]["id"]

    patched = await client.patch(
        f"/api/v1/agent/sessions/{session_id}",
        json={"approval_mode": "assist"},
        headers=auth_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["approval_mode"] == "assist"

    from sqlalchemy import func, select
    from app.models.audit_log import AuditLog

    count = await db_session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "update_session_approval_mode"
        )
    )
    assert count == 1

    same = await client.patch(
        f"/api/v1/agent/sessions/{session_id}",
        json={"approval_mode": "assist"},
        headers=auth_headers,
    )
    assert same.status_code == 200
    count_after = await db_session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "update_session_approval_mode"
        )
    )
    assert count_after == 1


async def test_patch_approval_mode_rejects_invalid_and_non_owner(
    client: AsyncClient,
    db_session: AsyncSession,
    test_role: Role,
    auth_headers: Headers,
    login_user,
) -> None:
    create_resp = await client.post(
        "/api/v1/agent/sessions",
        json={"title": "他人"},
        headers=auth_headers,
    )
    session_id = create_resp.json()["data"]["id"]

    bad = await client.patch(
        f"/api/v1/agent/sessions/{session_id}",
        json={"approval_mode": "bypass"},
        headers=auth_headers,
    )
    assert bad.status_code == 422

    other = await _other_user(db_session, test_role)
    other_headers = await login_user(other.username, "testpassword123")
    forbidden = await client.patch(
        f"/api/v1/agent/sessions/{session_id}",
        json={"approval_mode": "full"},
        headers=other_headers,
    )
    assert forbidden.status_code == 404


async def test_list_messages_root_transcript_only(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    test_role: Role,
    auth_headers: Headers,
    login_user,
) -> None:
    """消息历史只返回根 transcript（agent_id=None），且按 id 升序。"""
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "历史会话", "status": "active"},
    )
    await db_session.flush()

    await agent_message_crud.append(
        db_session,
        session_id=session.id,
        role="user",
        content="第一问",
        agent_id=None,
    )
    await agent_message_crud.append(
        db_session,
        session_id=session.id,
        role="assistant",
        content="第一答",
        agent_id=None,
        tool_calls=[{"id": "c1", "name": "lookup", "arguments": "{}"}],
    )
    await agent_message_crud.append(
        db_session,
        session_id=session.id,
        role="assistant",
        content="子 Agent 私有消息",
        agent_id="child-uuid-1",
    )
    await db_session.commit()

    response = await client.get(
        f"/api/v1/agent/sessions/{session.id}/messages",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    messages = response.json()["data"]
    assert len(messages) == 2
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert [m["content"] for m in messages] == ["第一问", "第一答"]
    assert messages[0]["id"] < messages[1]["id"]
    assert messages[1]["tool_calls"] == [
        {"id": "c1", "name": "lookup", "arguments": "{}"}
    ]
    assert "created_at" in messages[0]

    other = await _other_user(db_session, test_role)
    other_headers = await login_user(other.username, "testpassword123")
    denied = await client.get(
        f"/api/v1/agent/sessions/{session.id}/messages",
        headers=other_headers,
    )
    assert denied.status_code == 404


async def test_delete_session_hard_and_cascade(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    test_role: Role,
    auth_headers: Headers,
    login_user,
) -> None:
    """所有者可硬删会话；消息级联删除；非所有者 404。"""
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "待删除", "status": "active"},
    )
    await db_session.flush()
    await agent_message_crud.append(
        db_session,
        session_id=session.id,
        role="user",
        content="将被级联删除",
        agent_id=None,
    )
    await db_session.commit()
    session_id = session.id

    other = await _other_user(db_session, test_role)
    other_headers = await login_user(other.username, "testpassword123")
    forbidden = await client.delete(
        f"/api/v1/agent/sessions/{session_id}",
        headers=other_headers,
    )
    assert forbidden.status_code == 404

    deleted = await client.delete(
        f"/api/v1/agent/sessions/{session_id}",
        headers=auth_headers,
    )
    assert deleted.status_code == 200, deleted.text

    missing = await client.get(
        f"/api/v1/agent/sessions/{session_id}",
        headers=auth_headers,
    )
    assert missing.status_code == 404

    messages = await agent_message_crud.list_for_agent(
        db_session,
        session_id,
        agent_id=None,
    )
    assert messages == []


async def test_session_endpoints_require_agent_use_permission(
    client: AsyncClient,
    db_session: AsyncSession,
    login_user,
) -> None:
    """没有 agent:use 权限的用户不能创建/查看运维助手会话。"""
    role = Role(name="无权限角色", description="", permissions=[])
    db_session.add(role)
    await db_session.flush()
    user = User(
        username="nopermsession",
        email="nopermsession@example.com",
        hashed_password=hash_password("testpassword123"),
        nickname="无权限用户",
        is_active=True,
        is_superuser=False,
        roles=[role],
    )
    db_session.add(user)
    await db_session.commit()

    headers = await login_user("nopermsession", "testpassword123")

    create_resp = await client.post(
        "/api/v1/agent/sessions", json={"title": "无权限"}, headers=headers
    )
    assert create_resp.status_code == 403

    list_resp = await client.get("/api/v1/agent/sessions", headers=headers)
    assert list_resp.status_code == 403
