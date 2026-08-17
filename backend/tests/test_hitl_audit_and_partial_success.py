"""HITL 审计完整性与「审批成功但执行失败」语义的回归测试。

这三件事之前都是缺口，且都不会被功能测试发现——审批照常工作、执行照常
执行，只是事后查不到「谁从哪批的」「有没有人反复重试过」，以及部分成功时
前端会卡在死循环里。所以单独立一个文件把它们钉死。
"""

from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.audit_log import AuditLog
from app.models.permission import Permission
from app.models.role import role_permissions
from app.models.user import User, user_roles

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]
type LoginUser = Callable[[str, str], Awaitable[Headers]]


async def _grant_hitl_permission(db_session: AsyncSession, test_user: User) -> None:
    permission = Permission(
        name="审批 HITL 提案", code="agent:hitl_approve", module="Agent"
    )
    db_session.add(permission)
    await db_session.flush()
    role_id = (
        await db_session.execute(
            select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id)
        )
    ).scalar_one()
    await db_session.execute(
        role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
    )
    await db_session.commit()


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    from app.models.agent_session import AgentSession

    session = AgentSession(user_id=user_id, title="审计测试", status="active")
    db_session.add(session)
    await db_session.flush()
    await db_session.commit()
    return session.id


async def _make_notify_proposal(db_session: AsyncSession, session_id: int) -> int:
    """建一条 notify 提案——它不碰设备，执行路径最短，适合验审计。"""
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "测试通知", "proposal_reason": "回归测试"},
    )
    await db_session.commit()
    return proposal.id


async def _audit_rows(db_session: AsyncSession, action: str) -> list[AuditLog]:
    rows = await db_session.execute(
        select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.asc())
    )
    return list(rows.scalars().all())


async def test_manual_approval_records_source_ip(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """人工审批必须记录来源 IP。

    这是审计链上最需要溯源的动作——之前 HITL 全链路 5 处 log_audit 一处都没带 ip，
    而其它模块有 45 处带。事后追查「谁从哪台机器批准了这次重启」时只有 user_id。
    """
    await _grant_hitl_permission(db_session, test_user)
    session_id = await _make_session(db_session, test_user.id)
    proposal_id = await _make_notify_proposal(db_session, session_id)

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text

    approved = await _audit_rows(db_session, "hitl_approved")
    assert len(approved) == 1
    assert approved[0].ip != "", "人工审批的审计记录必须带来源 IP"
    assert approved[0].user_id == test_user.id


async def test_rejection_records_source_ip(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    await _grant_hitl_permission(db_session, test_user)
    session_id = await _make_session(db_session, test_user.id)
    proposal_id = await _make_notify_proposal(db_session, session_id)

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": False},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text

    rejected = await _audit_rows(db_session, "hitl_rejected")
    assert len(rejected) == 1
    assert rejected[0].ip != ""


async def test_auto_approval_is_distinguishable_from_manual(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """档位自动批准写 hitl_auto_approved 并带档位，与人工批准可区分。

    两者之前完全一致（同 action、同 user_id、status_reason 都是 None），
    事后无法回答「这条 reboot 是人工批的还是因为会话开着 full 档被自动批的」，
    而这两件事的责任性质完全不同。
    """
    from app.agent.hitl import decide_proposal

    session_id = await _make_session(db_session, test_user.id)
    proposal_id = await _make_notify_proposal(db_session, session_id)

    await decide_proposal(
        db_session,
        proposal_id=proposal_id,
        approve=True,
        reviewed_by_user_id=test_user.id,
        actor_ip="agent",
        auto_approval_mode="full",
    )
    await db_session.commit()

    assert await _audit_rows(db_session, "hitl_approved") == []
    auto = await _audit_rows(db_session, "hitl_auto_approved")
    assert len(auto) == 1
    assert "full" in auto[0].detail, "自动批准的审计必须带触发它的档位"
    assert auto[0].ip == "agent", "自动批准没有请求上下文，用固定标记与真实 IP 区分"


async def test_retry_writes_audit_even_when_it_fails(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """重试**失败**也要留痕。

    之前 retry 端点从头到尾没有 log_audit：预检失败时提案保持 APPROVED、
    执行阶段的审计一条都不写，于是管理员可以对同一条提案反复尝试直到某次成功，
    日志里只留下最后成功的那条。
    """
    await _grant_hitl_permission(db_session, test_user)
    session_id = await _make_session(db_session, test_user.id)
    # 指向不存在的资产，让预检必然失败
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_query",
        action_payload={
            "asset_id": 999999,
            "command_name": "show_version",
            "proposal_reason": "回归测试",
        },
    )
    await hitl_proposal_crud.decide(
        db_session, proposal.id, approve=True, reviewed_by_user_id=test_user.id
    )
    await db_session.commit()
    proposal_id = proposal.id

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/retry",
        json={},
        headers=auth_headers,
    )
    # 预检失败不认领，提案保持 APPROVED；HTTP 语义如何不是本例关注点
    assert response.status_code in (200, 409), response.text

    retries = await _audit_rows(db_session, "hitl_retry_requested")
    assert len(retries) == 1, "重试无论成败都必须留下审计"
    assert retries[0].ip != ""
    assert retries[0].user_id == test_user.id


async def test_unknown_resolution_records_source_ip(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    await _grant_hitl_permission(db_session, test_user)
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "x", "proposal_reason": "r"},
    )
    await hitl_proposal_crud.decide(
        db_session, proposal.id, approve=True, reviewed_by_user_id=test_user.id
    )
    await hitl_proposal_crud.claim_execution(db_session, proposal.id)
    await hitl_proposal_crud.mark_unknown(
        db_session, proposal.id, reason="dispatch_outcome_unknown"
    )
    await db_session.commit()

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal.id}/resolve-unknown",
        json={"resolution": "confirm_executed"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text

    rows = await _audit_rows(db_session, "hitl_unknown_confirmed")
    assert len(rows) == 1
    assert rows[0].ip != ""


async def test_decide_returns_200_with_execution_error_when_execution_fails(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """审批成功但执行未启动时返回 200 + execution_error，不是 409。

    返回 409 会让前端显示「批准失败」，而提案其实已 APPROVED；用户再点批准
    只会因状态已变再拿一个 409——死循环。正确的表达是「已批准，执行失败，可重试」。
    """
    from app.agent import hitl as hitl_module

    await _grant_hitl_permission(db_session, test_user)
    session_id = await _make_session(db_session, test_user.id)
    proposal_id = await _make_notify_proposal(db_session, session_id)

    async def failing_resume(*args: object, **kwargs: object) -> object:
        raise hitl_module.HitlResumeError("执行器暂时不可用")

    import app.api.v1.hitl as hitl_api

    original = hitl_api.resume_proposal
    hitl_api.resume_proposal = failing_resume  # type: ignore[assignment]
    try:
        response = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True},
            headers=auth_headers,
        )
    finally:
        hitl_api.resume_proposal = original  # type: ignore[assignment]

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["status"] == "APPROVED", "审批确实成功了，状态必须如实反映"
    assert payload["execution_error"] == "执行器暂时不可用"

    # 审批的审计照常写入——执行失败不该抹掉「有人批准过」这个事实
    approved = await _audit_rows(db_session, "hitl_approved")
    assert len(approved) == 1


async def test_dynamic_password_is_not_echoed_in_validation_errors(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """校验失败时响应里不得出现动态口令明文。

    SecretStr 之外的第二层防护：main.py 的 422 处理器剥掉了 input 字段。
    """
    await _grant_hitl_permission(db_session, test_user)
    secret = "SUPER-SECRET-OTP-123456"

    response = await client.post(
        "/api/v1/hitl/proposals/1/decide",
        json={
            "approve": True,
            "dynamic_credential_password": secret,
            "unexpected_field": 1,  # 触发 extra="forbid" 校验失败
        },
        headers=auth_headers,
    )

    assert response.status_code == 422, response.text
    assert secret not in response.text
