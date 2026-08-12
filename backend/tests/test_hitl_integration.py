"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_hitl_integration.py
@DateTime: 2026-08-12 11:40
@Docs: T10 HITL 跨组件验收：根调度器提案 → HTTP 审批 → 审计与子角色隔离。

实现流程：
1. 通过 CMDB CRUD 准备真实资产，再用 build_root_tool_dispatcher 走 propose_remediation。
2. 校验工具结果仅含安全摘要（pending_approval），不含原始载荷秘密。
3. 授予 agent:hitl_approve 后经 HTTP decide 完成审批，断言状态机与审计动作。
4. device_control 在通知自动批准开启时仍强制 HITL；stub 失败保持 APPROVED 且二次审批 409。
5. 子角色调度器即使白名单污染也拒绝 propose_remediation，保证写路径仅根 Agent 可走。
"""

import re

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tool_dispatch import build_root_tool_dispatcher, build_tool_dispatcher
from app.core.config import settings
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.audit_log import AuditLog
from app.models.permission import Permission
from app.models.user import User, user_roles

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]

_PROPOSAL_ID_RE = re.compile(r"整改提案\s+(\d+)")


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


async def _make_session_and_asset(db: AsyncSession, user_id: int) -> tuple[int, int]:
    """创建 Agent 会话与 CMDB 资产，供根调度器提案使用。"""
    session = await agent_session_crud.create(
        db,
        {"user_id": user_id, "title": "HITL 集成测试", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "server",
            "hostname": "srv-hitl-int",
            "ip_address": "10.0.0.40",
            "business_system": "测试系统",
            "subnet_cidr": "",
        },
    )
    await db.flush()
    return session.id, asset.id


def _extract_proposal_id(content: str) -> int:
    """从工具安全摘要中解析提案 ID。"""
    match = _PROPOSAL_ID_RE.search(content)
    assert match is not None, content
    return int(match.group(1))


async def test_scenario_a_notify_manual_approve_end_to_end(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario A：关闭通知自动批准时，根工具提案 → 人工批准 → EXECUTED 并写审计。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    secret = "SECRET_PAYLOAD_TOKEN_NOTIFY_X9"
    session_id, asset_id = await _make_session_and_asset(db_session, test_user.id)
    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
        proposed_by_agent_id="root-agent",
    )

    tool_result = await dispatch(
        "propose_remediation",
        {
            "asset_id": asset_id,
            "action_type": "notify",
            "payload": {"message": secret},
            "reason": "口联异常需确认",
        },
    )
    assert tool_result.control == "pending_approval"
    assert secret not in tool_result.content
    proposal_id = _extract_proposal_id(tool_result.content)

    proposal = await hitl_proposal_crud.get(db_session, proposal_id)
    assert proposal is not None
    assert proposal.status == "PENDING"
    assert proposal.action_payload["message"] == secret
    await db_session.commit()

    await _grant_hitl_approve(db_session, test_user)
    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "EXECUTED"

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


async def test_scenario_b_device_control_forced_hitl_and_stub_stays_approved(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario B：即使通知自动批准开启，device_control 仍强制 HITL；stub 失败保 APPROVED。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", True)
    session_id, asset_id = await _make_session_and_asset(db_session, test_user.id)
    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
    )

    tool_result = await dispatch(
        "propose_remediation",
        {
            "asset_id": asset_id,
            "action_type": "device_control",
            "payload": {"command": "reboot"},
            "reason": "故障恢复",
        },
    )
    assert tool_result.control == "pending_approval"
    proposal_id = _extract_proposal_id(tool_result.content)

    proposal = await hitl_proposal_crud.get(db_session, proposal_id)
    assert proposal is not None
    assert proposal.status == "PENDING"
    await db_session.commit()

    await _grant_hitl_approve(db_session, test_user)
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


async def test_scenario_c_child_dispatcher_rejects_propose_remediation(
    db_session: AsyncSession,
) -> None:
    """Scenario C：子角色调度器不得暴露或执行 propose_remediation。"""
    dispatch = build_tool_dispatcher(
        db_session,
        ("query_monitor_status", "propose_remediation"),
    )

    result = await dispatch(
        "propose_remediation",
        {
            "asset_id": 1,
            "action_type": "notify",
            "payload": {"message": "越权尝试"},
            "reason": "子角色写隔离",
        },
    )

    assert result.control == "rejected"
    assert "未知工具" in result.content
