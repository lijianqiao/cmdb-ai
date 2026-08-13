"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_hitl_api.py
@DateTime: 2026-08-12 11:36
@Docs: 验证 HITL 提案查询与审批 HTTP API 的权限门控与状态机行为。
"""

from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.hitl import propose_action
from app.core.cmdb_credential import encrypt_credential_password
from app.core.config import settings
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.audit_log import AuditLog
from app.models.permission import Permission
from app.models.user import User, user_roles

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _grant_hitl_approve(db_session: AsyncSession, test_user: User) -> None:
    """将 agent:hitl_approve 挂到 test_user 已有角色上。"""
    from app.models.role import role_permissions

    permission = Permission(name="审批 HITL 提案", code="agent:hitl_approve", module="Agent")
    db_session.add(permission)
    await db_session.flush()

    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    await db_session.execute(
        role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
    )
    await db_session.commit()


async def _make_pending_proposal(
    db: AsyncSession,
    *,
    user_id: int,
    action_type: str,
    payload: dict[str, object],
    reason: str,
) -> tuple[int, int]:
    """创建 PENDING 提案并提交，供跨会话的 HTTP 请求读取。"""
    session = await agent_session_crud.create(
        db,
        {"user_id": user_id, "title": "HITL API 测试", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "server",
            "hostname": "srv-hitl-api",
            "ip_address": "10.0.0.30",
            "business_system": "测试系统",
            "subnet_cidr": "",
        },
    )
    await db.flush()
    summary = await propose_action(
        db,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type=action_type,  # type: ignore[arg-type]
        asset_id=asset.id,
        payload=payload,
        reason=reason,
        actor_user_id=user_id,
    )
    await db.commit()
    return session.id, summary.proposal_id


async def _make_pending_device_query_proposal(
    db: AsyncSession,
    *,
    user_id: int,
    credential_type: str = "dynamic",
) -> tuple[int, int]:
    """创建动态凭据资产的 PENDING device_query 提案。"""
    session = await agent_session_crud.create(
        db,
        {"user_id": user_id, "title": "HITL device_query 测试", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "switch",
            "hostname": "sw-hitl-api",
            "ip_address": "10.0.0.31",
            "vendor": "cisco_iosxe",
            "credential_type": credential_type,
            "credential_username": "admin",
            "credential_password_encrypted": None,
        },
    )
    await db.flush()
    summary = await propose_action(
        db,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="device_query",
        asset_id=asset.id,
        payload={"command_name": "show_version"},
        reason="查询设备版本",
        actor_user_id=user_id,
    )
    await db.commit()
    return session.id, summary.proposal_id


def _generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


async def test_list_proposals_without_permission_returns_403(
    client: AsyncClient,
    auth_headers: Headers,
) -> None:
    """未授予 agent:hitl_approve 时查询应被拒绝。"""
    response = await client.get(
        "/api/v1/hitl/proposals",
        params={"session_id": 1},
        headers=auth_headers,
    )
    assert response.status_code == 403, response.text


async def test_list_proposals_returns_approver_payload(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """列表应对审批人返回完整 action_payload。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    await _grant_hitl_approve(db_session, test_user)
    session_id, proposal_id = await _make_pending_proposal(
        db_session,
        user_id=test_user.id,
        action_type="notify",
        payload={"message": "口联异常"},
        reason="监控告警",
    )

    response = await client.get(
        "/api/v1/hitl/proposals",
        params={"session_id": session_id},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert len(items) == 1
    assert items[0]["id"] == proposal_id
    assert items[0]["status"] == "PENDING"
    assert items[0]["action_payload"]["message"] == "口联异常"
    assert items[0]["action_payload"]["proposal_reason"] == "监控告警"


async def test_approve_notify_executes_and_writes_audit(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准 notify 应执行到 EXECUTED，并写入审批与执行审计。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_proposal(
        db_session,
        user_id=test_user.id,
        action_type="notify",
        payload={"message": "人工批准通知"},
        reason="需要确认",
    )

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "EXECUTED"

    get_response = await client.get(
        f"/api/v1/hitl/proposals/{proposal_id}",
        headers=auth_headers,
    )
    assert get_response.status_code == 200, get_response.text
    assert get_response.json()["data"]["status"] == "EXECUTED"

    db_session.expire_all()
    actions = {
        row.action
        for row in (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target == f"hitl_proposal:{proposal_id}")
            )
        ).scalars()
    }
    assert "hitl_approved" in actions
    assert "hitl_executed" in actions or "hitl_notify_executed" in actions


async def test_approve_device_control_stays_approved_second_decide_conflicts(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未分类 device_control 批准后连接失败应保持 APPROVED；再次审批返回 409。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    await _grant_hitl_approve(db_session, test_user)
    ciphertext = encrypt_credential_password("whatever")
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "HITL API device_control", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "server",
            "hostname": "srv-hitl-ctrl",
            "ip_address": "10.0.0.32",
            "vendor": "linux",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password_encrypted": ciphertext,
        },
    )
    await db_session.flush()
    summary = await propose_action(
        db_session,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset.id,
        payload={"command_name": "reboot"},
        reason="故障恢复",
        actor_user_id=test_user.id,
    )
    await db_session.commit()
    proposal_id = summary.proposal_id

    with patch(
        "app.agent.executors._open_scrapli_connection",
        side_effect=ConnectionError("unreachable"),
    ):
        first = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True},
            headers=auth_headers,
        )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["status"] == "APPROVED"

    second = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True},
        headers=auth_headers,
    )
    assert second.status_code == 409, second.text


async def test_reject_does_not_resume(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拒绝提案只更新状态，不应调用 resume。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_proposal(
        db_session,
        user_id=test_user.id,
        action_type="notify",
        payload={"message": "将被拒绝"},
        reason="误报",
    )

    resume_mock = AsyncMock()
    monkeypatch.setattr("app.api.v1.hitl.resume_proposal", resume_mock)

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": False},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "REJECTED"
    resume_mock.assert_not_awaited()

    db_session.expire_all()
    proposal = await hitl_proposal_crud.get(db_session, proposal_id)
    assert proposal is not None
    assert proposal.status == "REJECTED"


async def test_decide_device_query_requires_password_for_dynamic_credential(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准动态凭据 device_query 时未提供密码应返回 422。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_device_query_proposal(
        db_session,
        user_id=test_user.id,
    )

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True},
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


async def test_decide_device_query_with_password_executes(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准动态凭据 device_query 并提供密码应执行成功，且响应不含密码。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_device_query_proposal(
        db_session,
        user_id=test_user.id,
    )

    fake_connection = AsyncMock()
    fake_connection.send_command = AsyncMock(
        return_value=type("Resp", (), {"result": "device output", "failed": False})()
    )
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        response = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True, "dynamic_credential_password": "one-time-pass"},
            headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "EXECUTED"
    assert "one-time-pass" not in response.text
