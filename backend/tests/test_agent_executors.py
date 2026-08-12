"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_executors.py
@DateTime: 2026-08-12
@Docs: T10 HITL 执行器单元测试（notify + device_control stub）。
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.executors import NotifyExecutor, NotImplementedExecutor
from app.models.audit_log import AuditLog
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_not_implemented_executor_always_fails() -> None:
    executor = NotImplementedExecutor()

    result = await executor.execute({"command": "reboot"})

    assert result.ok is False
    assert result.message == "device_control 执行器尚未接入"


async def test_notify_executor_writes_audit_and_succeeds(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    executor = NotifyExecutor()
    proposal_id = 42

    result = await executor.execute(
        db_session,
        proposal_id=proposal_id,
        payload={"message": "SW-12 离线"},
        actor_user_id=test_user.id,
    )
    await db_session.flush()

    assert result.ok is True
    logs = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "hitl_notify_executed")
        )
    ).scalars().all()
    assert len(logs) == 1
    assert logs[0].user_id == test_user.id
    assert logs[0].target == f"hitl_proposal:{proposal_id}"
    assert "SW-12 离线" in logs[0].detail


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": ""},
        {"message": "   "},
    ],
)
async def test_notify_executor_rejects_blank_message(
    db_session: AsyncSession,
    test_user: User,
    payload: dict[str, str],
) -> None:
    executor = NotifyExecutor()

    result = await executor.execute(
        db_session,
        proposal_id=1,
        payload=payload,
        actor_user_id=test_user.id,
    )
    await db_session.flush()

    assert result.ok is False
    logs = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "hitl_notify_executed")
        )
    ).scalars().all()
    assert logs == []
