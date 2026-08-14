"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_hitl_execution.py
@DateTime: 2026-08-14
@Docs: 验证 HITL 独立执行服务的策略复检、认领顺序与 UNKNOWN 语义。
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent.executors import ExecutionResult
from app.agent.hitl import HitlResumeError
from app.agent.hitl_execution import execute_approved_proposal
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import device_command_policy_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.cmdb_asset import CmdbAsset
from app.models.hitl_proposal import HitlProposal
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _approved_device_proposal(
    db: AsyncSession,
    user: User,
) -> tuple[HitlProposal, int]:
    session = await agent_session_crud.create(
        db,
        {"user_id": user.id, "title": "execution", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "network",
            "hostname": "switch-42",
            "ip_address": "10.0.0.42",
            "business_system": "test",
            "subnet_cidr": "",
            "vendor": "cisco_iosxe",
            "credential_type": "dynamic",
            "credential_username": "admin",
        },
    )
    proposal = await hitl_proposal_crud.create(
        db,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="device_query",
        action_payload={
            "asset_id": asset.id,
            "command_name": "show_version",
            "proposal_reason": "verify policy drift",
        },
    )
    await hitl_proposal_crud.decide(
        db, proposal.id, approve=True, reviewed_by_user_id=user.id
    )
    await db.commit()
    return proposal, asset.id


async def _approved_notify_proposal(
    db: AsyncSession,
    user: User,
) -> HitlProposal:
    session = await agent_session_crud.create(
        db,
        {"user_id": user.id, "title": "notify", "status": "active"},
    )
    proposal = await hitl_proposal_crud.create(
        db,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "test notification", "proposal_reason": "test"},
    )
    await hitl_proposal_crud.decide(
        db, proposal.id, approve=True, reviewed_by_user_id=user.id
    )
    await db.commit()
    return proposal


class RecordingDeviceExecutor:
    """记录设备执行器调用参数，供断言策略拦截与认领前失败。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str | None, str | None]] = []

    async def execute(
        self,
        db: AsyncSession,
        *,
        asset: CmdbAsset,
        command_name: str,
        dynamic_password: str | None,
        interface_name: str | None = None,
    ) -> ExecutionResult:
        self.calls.append(
            (asset.id, command_name, dynamic_password, interface_name)
        )
        return ExecutionResult(ok=True, message="ok")


async def test_blacklist_added_after_approval_blocks_execution(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    proposal, asset_id = await _approved_device_proposal(
        db_session, test_user
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_version",
            "decision": "blacklist",
        },
    )
    await db_session.commit()
    fake_device_executor = RecordingDeviceExecutor()
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    proposal_id = proposal.id

    summary = await execute_approved_proposal(
        session_factory=session_factory,
        proposal_id=proposal_id,
        actor_user_id=test_user.id,
        dynamic_password="one-use-password",
        device_executor=fake_device_executor,
    )

    assert summary.status == "REJECTED"
    assert fake_device_executor.calls == []
    db_session.expire_all()
    persisted = await hitl_proposal_crud.get(db_session, proposal_id)
    assert persisted is not None
    assert persisted.status_reason == "policy_blacklisted"


async def test_unknown_command_before_claim_stays_approved(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """命令定义不存在时不得认领，状态保持 APPROVED 且执行器不被调用。"""
    proposal, _ = await _approved_device_proposal(db_session, test_user)
    proposal.action_payload = {
        **proposal.action_payload,
        "command_name": "nonexistent_command_xyz",
    }
    await db_session.commit()

    fake_device_executor = RecordingDeviceExecutor()
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    summary = await execute_approved_proposal(
        session_factory=session_factory,
        proposal_id=proposal.id,
        actor_user_id=test_user.id,
        dynamic_password="one-use-password",
        device_executor=fake_device_executor,
    )

    assert summary.status == "APPROVED"
    assert fake_device_executor.calls == []
    persisted = await hitl_proposal_crud.get(db_session, proposal.id)
    assert persisted is not None
    assert persisted.status == "APPROVED"
    assert persisted.executed_at is None


async def test_dynamic_password_missing_before_claim_stays_approved(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """动态凭据未提供时不得认领，状态保持 APPROVED 且执行器不被调用。"""
    proposal, _ = await _approved_device_proposal(db_session, test_user)
    fake_device_executor = RecordingDeviceExecutor()
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    summary = await execute_approved_proposal(
        session_factory=session_factory,
        proposal_id=proposal.id,
        actor_user_id=test_user.id,
        dynamic_password=None,
        device_executor=fake_device_executor,
    )

    assert summary.status == "APPROVED"
    assert fake_device_executor.calls == []
    persisted = await hitl_proposal_crud.get(db_session, proposal.id)
    assert persisted is not None
    assert persisted.status == "APPROVED"


async def test_executor_observes_committed_executing_state(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    proposal = await _approved_notify_proposal(db_session, test_user)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    observed: list[str] = []

    class FakeExecutor:
        async def execute(self, db, *, proposal_id, payload, actor_user_id):
            async with session_factory() as observer:
                persisted = await observer.get(HitlProposal, proposal_id)
                assert persisted is not None
                observed.append(persisted.status)
            return ExecutionResult(ok=True, message="ok")

    result = await execute_approved_proposal(
        session_factory=session_factory,
        proposal_id=proposal.id,
        actor_user_id=test_user.id,
        notify_executor=FakeExecutor(),
    )
    assert observed == ["EXECUTING"]
    assert result.status == "EXECUTED"


async def test_executor_timeout_marks_unknown_and_blocks_retry(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """执行器超时应持久化为 UNKNOWN，再次调用执行服务应被拒绝。"""
    proposal = await _approved_notify_proposal(db_session, test_user)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    proposal_id = proposal.id
    user_id = test_user.id

    class TimeoutExecutor:
        async def execute(self, db, *, proposal_id, payload, actor_user_id):
            raise TimeoutError("simulated device timeout")

    summary = await execute_approved_proposal(
        session_factory=session_factory,
        proposal_id=proposal_id,
        actor_user_id=user_id,
        notify_executor=TimeoutExecutor(),
    )
    assert summary.status == "UNKNOWN"

    db_session.expire_all()
    persisted = await hitl_proposal_crud.get(db_session, proposal_id)
    assert persisted is not None
    assert persisted.status == "UNKNOWN"
    assert persisted.status_reason == "dispatch_outcome_unknown"

    with pytest.raises(HitlResumeError):
        await execute_approved_proposal(
            session_factory=session_factory,
            proposal_id=proposal_id,
            actor_user_id=user_id,
            notify_executor=TimeoutExecutor(),
        )
