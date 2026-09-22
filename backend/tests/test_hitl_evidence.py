"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_hitl_evidence.py
@DateTime: 2026-09-22
@Docs: R4：审批/执行证据独立于聊天生命周期保留，不能被级联删除，审计员可查。

实现流程：
1. 提案创建时记下申请人与当时的资产/命令快照，审批时记下审批方式（人工 / 档位自动）。
2. 数据库外键 RESTRICT：有提案的会话删不掉，有证据的用户永久删除返回冲突。
3. 审计员（audit:read）与审批人都能按提案 ID 查完整证据。
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.device_commands import DEVICE_COMMAND_CATALOG_VERSION
from app.agent.hitl import decide_proposal, gate_action
from app.core.security import hash_password
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.agent_session import AgentSession
from app.models.role import Role
from app.models.user import User

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _session_and_asset(db: AsyncSession, user_id: int) -> tuple[int, int]:
    session = await agent_session_crud.create(
        db, {"user_id": user_id, "title": "证据", "status": "active"}
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "switch",
            "hostname": "SW-EVIDENCE-01",
            "ip_address": "10.8.0.1",
            "vendor": "cisco_iosxe",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password_encrypted": "placeholder",
        },
    )
    await db.flush()
    return session.id, asset.id


async def test_proposal_records_requester_and_snapshot_at_creation(
    db_session: AsyncSession, test_user: User
) -> None:
    """资产之后可能改名、换 IP、换厂商：证据必须是提案当时的样子，不能事后按当前值拼。"""
    session_id, asset_id = await _session_and_asset(db_session, test_user.id)

    summary = await gate_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset_id,
        payload={"command_name": "port_disable", "interface_name": "GigabitEthernet0/1"},
        reason="端口异常",
        actor_user_id=test_user.id,
    )

    proposal = await hitl_proposal_crud.get(db_session, summary.proposal_id)
    assert proposal is not None
    assert proposal.requested_by_user_id == test_user.id
    snapshot = proposal.evidence_snapshot
    assert snapshot is not None
    assert snapshot["asset"] == {
        "id": asset_id,
        "hostname": "SW-EVIDENCE-01",
        "ip_address": "10.8.0.1",
        "asset_type": "switch",
        "vendor": "cisco_iosxe",
        "credential_type": "static",
    }
    assert snapshot["command"] == {
        "name": "port_disable",
        "type": "state_changing",
        "catalog_version": DEVICE_COMMAND_CATALOG_VERSION,
        "rendered": ["interface GigabitEthernet0/1", "shutdown"],
    }
    assert "password" not in str(snapshot).lower()


async def test_decision_records_how_it_was_approved(
    db_session: AsyncSession, test_user: User
) -> None:
    """人工批准与档位自动批准是两种责任，证据里要分得清。"""
    session_id, asset_id = await _session_and_asset(db_session, test_user.id)
    manual = await gate_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset_id,
        payload={"command_name": "reboot"},
        reason="人工批准",
        actor_user_id=test_user.id,
    )
    auto = await gate_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset_id,
        payload={"command_name": "reboot"},
        reason="档位自动批准",
        actor_user_id=test_user.id,
    )

    await decide_proposal(
        db_session, proposal_id=manual.proposal_id, approve=True, reviewed_by_user_id=test_user.id
    )
    await decide_proposal(
        db_session,
        proposal_id=auto.proposal_id,
        approve=True,
        reviewed_by_user_id=test_user.id,
        auto_approval_mode="full",
    )

    manual_row = await hitl_proposal_crud.get(db_session, manual.proposal_id)
    auto_row = await hitl_proposal_crud.get(db_session, auto.proposal_id)
    assert manual_row is not None and manual_row.approval_method == "manual"
    assert auto_row is not None and auto_row.approval_method == "auto:full"


async def test_session_with_proposals_cannot_be_deleted_at_database_level(
    db_session: AsyncSession, test_user: User
) -> None:
    """外键 RESTRICT：绕过服务层直接删会话也不会级联销毁审批证据。"""
    session_id, _ = await _session_and_asset(db_session, test_user.id)
    await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "留痕"},
    )
    await db_session.commit()

    with pytest.raises(IntegrityError):
        await db_session.execute(delete(AgentSession).where(AgentSession.id == session_id))
    await db_session.rollback()


async def _deleted_user_with_session(
    db: AsyncSession, role: Role, *, username: str, with_proposal: bool
) -> tuple[int, int]:
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=hash_password("testpassword123"),
        nickname=username,
        is_active=True,
        is_superuser=False,
        is_deleted=True,
        roles=[role],
    )
    db.add(user)
    await db.flush()
    session = await agent_session_crud.create(
        db, {"user_id": user.id, "title": "离职员工的会话", "status": "archived"}
    )
    await db.flush()
    if with_proposal:
        await hitl_proposal_crud.create(
            db,
            session_id=session.id,
            proposed_by_agent_id=None,
            action_type="notify",
            action_payload={"message": "留痕"},
        )
    await db.commit()
    return user.id, session.id


async def test_purging_user_with_evidence_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    test_role: Role,
    auth_headers: Headers,
) -> None:
    """保留期内有审批/执行证据的用户不能永久删除，返回 409 并说明原因。"""
    user_id, session_id = await _deleted_user_with_session(
        db_session, test_role, username="leaver1", with_proposal=True
    )

    response = await client.delete(f"/api/v1/users/{user_id}/purge", headers=auth_headers)

    assert response.status_code == 409, response.text
    assert "证据" in response.json()["message"]
    db_session.expire_all()
    assert await agent_session_crud.get(db_session, session_id) is not None


async def test_purging_user_who_reviewed_others_proposals_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    test_role: Role,
    auth_headers: Headers,
) -> None:
    """审批人字段是 SET NULL：直接删掉审批人，别人提案上「谁批的」就悄悄没了，同样要拒绝。"""
    reviewer_id, _ = await _deleted_user_with_session(
        db_session, test_role, username="reviewer1", with_proposal=False
    )
    session_id, _ = await _session_and_asset(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "别人的提案"},
    )
    proposal.status = "REJECTED"
    proposal.reviewed_by_user_id = reviewer_id
    await db_session.commit()

    response = await client.delete(f"/api/v1/users/{reviewer_id}/purge", headers=auth_headers)

    assert response.status_code == 409, response.text
    assert "证据" in response.json()["message"]


async def test_purging_user_without_evidence_still_works(
    client: AsyncClient,
    db_session: AsyncSession,
    test_role: Role,
    auth_headers: Headers,
) -> None:
    """没有审批证据的聊天随用户一起清理，不影响原有的永久删除。"""
    user_id, session_id = await _deleted_user_with_session(
        db_session, test_role, username="leaver2", with_proposal=False
    )

    response = await client.delete(f"/api/v1/users/{user_id}/purge", headers=auth_headers)

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert await agent_session_crud.get(db_session, session_id) is None


async def test_auditor_can_read_proposal_evidence(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """审计员（audit:read）可以按审计项里的提案 ID 查到完整证据，不必持有审批权限。"""
    session_id, asset_id = await _session_and_asset(db_session, test_user.id)
    summary = await gate_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset_id,
        payload={"command_name": "reboot"},
        reason="审计抽查",
        actor_user_id=test_user.id,
    )
    await db_session.commit()

    # 管理员角色夹具自带 audit:read，但没有 agent:hitl_approve
    response = await client.get(
        f"/api/v1/hitl/proposals/{summary.proposal_id}", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["requested_by_user_id"] == test_user.id
    assert data["evidence_snapshot"]["asset"]["hostname"] == "SW-EVIDENCE-01"


async def test_proposal_evidence_needs_audit_or_approval_permission(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    login_user,
) -> None:
    session_id, asset_id = await _session_and_asset(db_session, test_user.id)
    summary = await gate_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset_id,
        payload={"command_name": "reboot"},
        reason="无权查看",
        actor_user_id=test_user.id,
    )
    role = Role(name="无关角色", description="", permissions=[])
    outsider = User(
        username="outsider",
        email="outsider@example.com",
        hashed_password=hash_password("testpassword123"),
        nickname="outsider",
        is_active=True,
        is_superuser=False,
        roles=[role],
    )
    db_session.add_all([role, outsider])
    await db_session.commit()
    headers = await login_user("outsider", "testpassword123")

    response = await client.get(f"/api/v1/hitl/proposals/{summary.proposal_id}", headers=headers)

    assert response.status_code == 403
