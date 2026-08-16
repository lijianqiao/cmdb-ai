"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_hitl_execution.py
@DateTime: 2026-08-14
@Docs: 验证 HITL 独立执行服务的策略复检、认领顺序与 UNKNOWN 语义。
"""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent import hitl_execution
from app.agent.executors import ExecutionResult
from app.agent.hitl import HitlResumeError, ProposalSafeSummary, resume_proposal
from app.agent.hitl_execution import (
    _preflight_and_claim,
    build_result_preview,
    execute_approved_proposal,
)
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import device_command_policy_crud
from app.crud.hitl_execution_result import hitl_execution_result_crud
from app.crud.hitl_proposal import InvalidHitlTransitionError, hitl_proposal_crud
from app.models.agent_session import AgentSession
from app.models.audit_log import AuditLog
from app.models.cmdb_asset import CmdbAsset
from app.models.hitl_proposal import HitlProposal
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _approved_device_proposal(
    db: AsyncSession,
    user: User,
    *,
    action_type: str = "device_query",
    command_name: str = "show_version",
    vendor: str = "cisco_iosxe",
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
            "vendor": vendor,
            "credential_type": "dynamic",
            "credential_username": "admin",
        },
    )
    proposal = await hitl_proposal_crud.create(
        db,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type=action_type,
        action_payload={
            "asset_id": asset.id,
            "command_name": command_name,
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


class OutputDeviceExecutor:
    """返回受控假设备输出，验证执行收尾的数据分流。"""

    def __init__(self, output: str) -> None:
        self.output = output

    async def execute(
        self,
        db: AsyncSession,
        *,
        asset: CmdbAsset,
        command_name: str,
        dynamic_password: str | None,
        interface_name: str | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            ok=True,
            message="ok",
            detail={"output": self.output, "truncated": False},
            dispatched=True,
        )


async def test_build_result_preview_preserves_at_limit_and_marks_long_output() -> None:
    """预览在边界内原样返回，越界时必须附上固定截断标记。"""
    assert build_result_preview("A" * 4000) == "A" * 4000
    assert build_result_preview("A" * 4001) == "A" * 4000 + "…(截断)"


async def test_device_query_persists_full_output_and_repeated_resume_is_idempotent(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查询正文只入专用表；摘要和重复恢复都只能看到安全预览及存在标志。"""
    raw_output = "<H3C>display current-configuration\n" + "A" * 5000
    proposal, _ = await _approved_device_proposal(
        db_session,
        test_user,
        command_name="show_running_config",
        vendor="hp_comware",
    )
    proposal_id = proposal.id
    user_id = test_user.id
    fake_executor = OutputDeviceExecutor(raw_output)
    monkeypatch.setattr(hitl_execution, "DeviceQueryExecutor", lambda: fake_executor)

    summary = await resume_proposal(
        db_session,
        proposal_id=proposal_id,
        actor_user_id=user_id,
        dynamic_password="one-use-password",
    )
    repeated_summary = await resume_proposal(
        db_session,
        proposal_id=proposal_id,
        actor_user_id=user_id,
        dynamic_password="one-use-password",
    )

    expected_preview = raw_output[:4000] + "…(截断)"
    assert summary.status == "EXECUTED"
    assert summary.result_excerpt == expected_preview
    assert summary.has_full_result is True
    assert repeated_summary.has_full_result is True
    db_session.expire_all()
    persisted = await hitl_proposal_crud.get(db_session, proposal_id)
    assert persisted is not None
    assert persisted.status == "EXECUTED"
    assert persisted.action_payload["last_result_excerpt"] == expected_preview
    result_row = await hitl_execution_result_crud.get_by_proposal(db_session, proposal_id)
    assert result_row is not None
    assert result_row.content == raw_output
    assert result_row.content_length == len(raw_output)


async def test_device_control_only_persists_preview_without_full_result(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """会改变状态的 device_control 维持既有语义，不得写入查询正文表。"""
    raw_output = "control completed"
    proposal, _ = await _approved_device_proposal(
        db_session,
        test_user,
        action_type="device_control",
        command_name="reboot",
    )
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    summary = await execute_approved_proposal(
        session_factory=session_factory,
        proposal_id=proposal.id,
        actor_user_id=test_user.id,
        dynamic_password="one-use-password",
        device_executor=OutputDeviceExecutor(raw_output),
    )

    assert summary.result_excerpt == raw_output
    assert summary.has_full_result is False
    result_row = await hitl_execution_result_crud.get_by_proposal(db_session, proposal.id)
    assert result_row is None


async def test_execution_result_rows_cascade_when_proposal_or_session_is_deleted(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """删除提案或所属会话时，数据库外键必须删除其完整查询结果。"""
    proposal, _ = await _approved_device_proposal(db_session, test_user)
    await hitl_execution_result_crud.create_for_proposal(
        db_session,
        proposal_id=proposal.id,
        content="proposal result",
    )
    await db_session.commit()
    await db_session.delete(proposal)
    await db_session.commit()
    assert await hitl_execution_result_crud.get_by_proposal(db_session, proposal.id) is None

    session_proposal, _ = await _approved_device_proposal(db_session, test_user)
    await hitl_execution_result_crud.create_for_proposal(
        db_session,
        proposal_id=session_proposal.id,
        content="session result",
    )
    await db_session.commit()
    session = await db_session.get(AgentSession, session_proposal.session_id)
    assert session is not None
    await db_session.delete(session)
    await db_session.commit()
    assert (
        await hitl_execution_result_crud.get_by_proposal(db_session, session_proposal.id)
        is None
    )


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


async def test_finalization_failure_marks_unknown(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部执行成功但终态落库失败时，提案必须进入 UNKNOWN。"""
    proposal = await _approved_notify_proposal(db_session, test_user)
    proposal_id = proposal.id
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    class SuccessfulExecutor:
        async def execute(self, db, *, proposal_id, payload, actor_user_id):
            return ExecutionResult(ok=True, message="dispatched")

    async def fail_finalization(db: AsyncSession, proposal_id: int) -> HitlProposal:
        raise RuntimeError("simulated finalization failure")

    monkeypatch.setattr(hitl_proposal_crud, "mark_executed", fail_finalization)

    summary = await execute_approved_proposal(
        session_factory=session_factory,
        proposal_id=proposal_id,
        actor_user_id=test_user.id,
        notify_executor=SuccessfulExecutor(),
    )

    assert summary.status == "UNKNOWN"
    db_session.expire_all()
    persisted = await hitl_proposal_crud.get(db_session, proposal_id)
    assert persisted is not None
    assert persisted.status == "UNKNOWN"
    assert persisted.status_reason == "dispatch_outcome_unknown"


async def test_finalization_cancellation_marks_unknown_before_reraising(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收尾阶段被取消时仍要先持久化 UNKNOWN，再向调用方传播取消。"""
    proposal = await _approved_notify_proposal(db_session, test_user)
    proposal_id = proposal.id
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    class SuccessfulExecutor:
        async def execute(self, db, *, proposal_id, payload, actor_user_id):
            return ExecutionResult(ok=True, message="dispatched")

    async def cancel_finalization(db: AsyncSession, proposal_id: int) -> HitlProposal:
        raise asyncio.CancelledError

    monkeypatch.setattr(hitl_proposal_crud, "mark_executed", cancel_finalization)

    with pytest.raises(asyncio.CancelledError):
        await execute_approved_proposal(
            session_factory=session_factory,
            proposal_id=proposal_id,
            actor_user_id=test_user.id,
            notify_executor=SuccessfulExecutor(),
        )

    db_session.expire_all()
    persisted = await hitl_proposal_crud.get(db_session, proposal_id)
    assert persisted is not None
    assert persisted.status == "UNKNOWN"
    assert persisted.status_reason == "dispatch_outcome_unknown"


async def test_unknown_execution_writes_audit(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """UNKNOWN 路径应写入审计，风格与成功路径一致。"""
    proposal = await _approved_notify_proposal(db_session, test_user)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    proposal_id = proposal.id

    class TimeoutExecutor:
        async def execute(self, db, *, proposal_id, payload, actor_user_id):
            raise TimeoutError("simulated device timeout")

    await execute_approved_proposal(
        session_factory=session_factory,
        proposal_id=proposal_id,
        actor_user_id=test_user.id,
        notify_executor=TimeoutExecutor(),
    )

    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.target == f"hitl_proposal:{proposal_id}")
        )
    ).scalars().all()
    actions = {row.action for row in rows}
    assert "hitl_execution_unknown" in actions
    unknown_row = next(row for row in rows if row.action == "hitl_execution_unknown")
    assert unknown_row.detail == "动作类型：notify"
    assert "TimeoutError" not in unknown_row.detail
    assert "password" not in unknown_row.detail.lower()


async def test_claim_loser_refreshes_executed_without_crash(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发认领失败时应刷新状态，EXECUTED 直接返回摘要而非崩溃。"""
    proposal = await _approved_notify_proposal(db_session, test_user)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    proposal_id = proposal.id
    original_claim = hitl_proposal_crud.claim_execution

    async def losing_claim(db: AsyncSession, pid: int) -> HitlProposal:
        async with session_factory() as setup_db:
            await original_claim(setup_db, pid)
            await hitl_proposal_crud.mark_executed(setup_db, pid)
            await setup_db.commit()
        raise InvalidHitlTransitionError("EXECUTING", "EXECUTING")

    monkeypatch.setattr(hitl_proposal_crud, "claim_execution", losing_claim)

    result = await _preflight_and_claim(
        session_factory=session_factory,
        proposal_id=proposal_id,
        dynamic_password=None,
        publisher=None,
    )

    assert isinstance(result, ProposalSafeSummary)
    assert result.status == "EXECUTED"


async def test_claim_loser_polls_executing_until_done(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """认领失败且状态为 EXECUTING 时应轮询等待，不得标记 UNKNOWN。"""
    import asyncio

    proposal = await _approved_notify_proposal(db_session, test_user)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    proposal_id = proposal.id

    async with session_factory() as db:
        await hitl_proposal_crud.claim_execution(db, proposal_id)
        await db.commit()

    async def finish_execution() -> None:
        await asyncio.sleep(0.15)
        async with session_factory() as finish_db:
            await hitl_proposal_crud.mark_executed(finish_db, proposal_id)
            await finish_db.commit()

    finish_task = asyncio.create_task(finish_execution())

    result = await _preflight_and_claim(
        session_factory=session_factory,
        proposal_id=proposal_id,
        dynamic_password=None,
        publisher=None,
    )
    await finish_task

    assert isinstance(result, ProposalSafeSummary)
    assert result.status == "EXECUTED"
    db_session.expire_all()
    persisted = await hitl_proposal_crud.get(db_session, proposal_id)
    assert persisted is not None
    assert persisted.status == "EXECUTED"
    assert persisted.status != "UNKNOWN"
