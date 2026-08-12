"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_hitl.py
@DateTime: 2026-08-12 11:15
@Docs: 验证 HITL 编排层的严格校验、状态迁移、执行幂等性与安全事件。
"""

import asyncio
from collections.abc import Mapping
from dataclasses import asdict

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent import hitl
from app.agent.executors import ExecutionResult
from app.agent.hitl import (
    HitlProposalRejectedError,
    HitlResumeError,
    ProposalSafeSummary,
    decide_proposal,
    propose_action,
    resume_proposal,
)
from app.core.config import settings
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.audit_log import AuditLog
from app.models.hitl_proposal import HitlProposal
from app.models.user import User

pytestmark = pytest.mark.asyncio


class RecordingPublisher:
    """记录发布事件，供编排测试断言事件类型和安全载荷。"""

    def __init__(self) -> None:
        """初始化空事件列表。"""
        self.events: list[tuple[int, str, dict[str, object]]] = []

    async def publish(
        self,
        *,
        session_id: int,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        """保存事件快照，避免后续对象变化影响断言。"""
        self.events.append((session_id, event_type, dict(payload)))


async def _make_context(db: AsyncSession, user_id: int) -> tuple[int, int]:
    """创建一组可用于提案的会话和 CMDB 资产。"""
    session = await agent_session_crud.create(
        db,
        {"user_id": user_id, "title": "HITL 测试", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "server",
            "hostname": "srv-hitl",
            "ip_address": "10.0.0.20",
            "business_system": "测试系统",
            "subnet_cidr": "",
        },
    )
    await db.flush()
    return session.id, asset.id


async def _proposal_count(db: AsyncSession) -> int:
    """返回当前事务中的 HITL 提案总数。"""
    result = await db.execute(select(func.count()).select_from(HitlProposal))
    return int(result.scalar_one())


async def test_propose_merges_matching_asset_id_and_returns_safe_summary(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冗余且匹配的 asset_id 应被剥离、校验并安全合并存储。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    session_id, asset_id = await _make_context(db_session, test_user.id)
    publisher = RecordingPublisher()

    summary = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        asset_id=asset_id,
        payload={"asset_id": asset_id, "message": "设备离线，请检查口联"},
        reason="监控告警",
        actor_user_id=test_user.id,
        publisher=publisher,
    )

    proposal = await hitl_proposal_crud.get(db_session, summary.proposal_id)
    assert proposal is not None
    assert proposal.action_payload == {
        "message": "设备离线，请检查口联",
        "asset_id": asset_id,
        "proposal_reason": "监控告警",
    }
    assert summary.status == "PENDING"
    assert set(asdict(summary)) == {"proposal_id", "action_type", "status", "reason", "asset_id"}
    assert [event[1] for event in publisher.events] == ["hitl_pending"]
    assert set(publisher.events[0][2]) == {
        "proposal_id",
        "action_type",
        "status",
        "reason",
        "asset_id",
    }


@pytest.mark.parametrize(
    ("action_type", "payload"),
    [
        ("unknown", {"message": "告警"}),
        ("notify", {"message": "告警", "secret": "不得接收"}),
        ("notify", {"message": 123}),
        ("device_control", {"command": 123}),
    ],
)
async def test_propose_rejects_invalid_payload_before_insert(
    db_session: AsyncSession,
    test_user: User,
    action_type: str,
    payload: dict[str, object],
) -> None:
    """未知动作、额外字段和严格模式类型错误都不得创建提案。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)

    with pytest.raises(HitlProposalRejectedError) as exc_info:
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type=action_type,  # type: ignore[arg-type]
            asset_id=asset_id,
            payload=payload,
            reason="测试非法输入",
            actor_user_id=test_user.id,
        )

    assert "不得接收" not in str(exc_info.value)
    assert "input_value" not in str(exc_info.value)
    assert await _proposal_count(db_session) == 0


async def test_propose_rejection_omits_extra_secret_field_value(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """L2 校验拒绝时，额外密钥字段值不得出现在面向 Agent 的错误文本中。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)
    secret = "SECRET_PAYLOAD_TOKEN_REJECT_X9"

    with pytest.raises(HitlProposalRejectedError) as exc_info:
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="notify",
            asset_id=asset_id,
            payload={"message": "告警", "password": secret},
            reason="额外密钥字段回归",
            actor_user_id=test_user.id,
        )

    message = str(exc_info.value)
    assert secret not in message
    assert "input_value" not in message
    assert "password" in message
    assert "校验失败" in message
    assert await _proposal_count(db_session) == 0


async def test_propose_rejects_mismatched_asset_id_before_insert(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """payload 内 asset_id 与顶层参数冲突时应明确拒绝。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)

    with pytest.raises(HitlProposalRejectedError, match="asset_id"):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="notify",
            asset_id=asset_id,
            payload={"asset_id": asset_id + 1, "message": "告警"},
            reason="冲突资产",
            actor_user_id=test_user.id,
        )

    assert await _proposal_count(db_session) == 0


async def test_propose_rejects_missing_cmdb_asset_without_insert(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """CMDB 中不存在的资产不得进入审批队列。"""
    session_id = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "", "status": "active"},
    )
    await db_session.flush()

    with pytest.raises(HitlProposalRejectedError, match="CMDB"):
        await propose_action(
            db_session,
            session_id=session_id.id,
            proposed_by_agent_id=None,
            action_type="notify",
            asset_id=999_999,
            payload={"message": "告警"},
            reason="资产不存在",
            actor_user_id=test_user.id,
        )

    assert await _proposal_count(db_session) == 0


async def test_propose_rejects_non_integer_actor_before_insert(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """自动审批身份必须来自真实整数用户 ID，不接受空值或合成占位。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)

    with pytest.raises(HitlProposalRejectedError, match="actor_user_id"):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="notify",
            asset_id=asset_id,
            payload={"message": "告警"},
            reason="身份测试",
            actor_user_id=None,  # type: ignore[arg-type]
        )

    assert await _proposal_count(db_session) == 0


async def test_notify_auto_approve_uses_actor_and_executes_once(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify 自动审批应使用真实操作者并在一次调用内执行完成。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", True)
    session_id, asset_id = await _make_context(db_session, test_user.id)
    publisher = RecordingPublisher()

    summary = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        asset_id=asset_id,
        payload={"message": "自动通知"},
        reason="低风险告警",
        actor_user_id=test_user.id,
        publisher=publisher,
    )

    proposal = await hitl_proposal_crud.get(db_session, summary.proposal_id)
    assert proposal is not None
    assert summary.status == "EXECUTED"
    assert proposal.reviewed_by_user_id == test_user.id
    assert proposal.executed_at is not None
    assert [event[1] for event in publisher.events] == ["hitl_pending", "hitl_resolved"]

    repeated = await resume_proposal(
        db_session,
        proposal_id=proposal.id,
        actor_user_id=test_user.id,
        publisher=publisher,
    )
    assert repeated == summary
    assert [event[1] for event in publisher.events] == ["hitl_pending", "hitl_resolved"]
    audit_count = await db_session.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "hitl_notify_executed")
    )
    assert audit_count.scalar_one() == 1


async def test_device_control_never_auto_approves(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """高风险 device_control 即使配置开启也必须保持 PENDING。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", True)
    session_id, asset_id = await _make_context(db_session, test_user.id)

    summary = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset_id,
        payload={"command": "reboot"},
        reason="故障恢复",
        actor_user_id=test_user.id,
    )

    assert summary.status == "PENDING"


async def test_decide_approve_does_not_resume_or_resolve(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工批准只改变状态，不应隐式执行或发布 resolved。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    session_id, asset_id = await _make_context(db_session, test_user.id)
    proposal = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        asset_id=asset_id,
        payload={"message": "待人工批准"},
        reason="人工确认",
        actor_user_id=test_user.id,
    )
    publisher = RecordingPublisher()

    approved = await decide_proposal(
        db_session,
        proposal_id=proposal.proposal_id,
        approve=True,
        reviewed_by_user_id=test_user.id,
        publisher=publisher,
    )

    assert approved.status == "APPROVED"
    assert publisher.events == []


async def test_decide_reject_publishes_resolved(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工拒绝进入终态时应发布一次 resolved。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    session_id, asset_id = await _make_context(db_session, test_user.id)
    proposal = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        asset_id=asset_id,
        payload={"message": "拒绝通知"},
        reason="人工确认",
        actor_user_id=test_user.id,
    )
    publisher = RecordingPublisher()

    rejected = await decide_proposal(
        db_session,
        proposal_id=proposal.proposal_id,
        approve=False,
        reviewed_by_user_id=test_user.id,
        publisher=publisher,
    )

    assert rejected.status == "REJECTED"
    assert [event[1] for event in publisher.events] == ["hitl_resolved"]


@pytest.mark.parametrize("reject_first", [False, True])
async def test_resume_rejects_pending_and_rejected(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
    reject_first: bool,
) -> None:
    """PENDING 与 REJECTED 均不允许绕过批准直接执行。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    session_id, asset_id = await _make_context(db_session, test_user.id)
    proposal = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        asset_id=asset_id,
        payload={"message": "不可执行"},
        reason="状态保护",
        actor_user_id=test_user.id,
    )
    if reject_first:
        await decide_proposal(
            db_session,
            proposal_id=proposal.proposal_id,
            approve=False,
            reviewed_by_user_id=test_user.id,
        )

    with pytest.raises(HitlResumeError):
        await resume_proposal(
            db_session,
            proposal_id=proposal.proposal_id,
            actor_user_id=test_user.id,
        )


async def test_concurrent_decide_one_winner_never_clobbers_executed(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """两个独立会话并发 approve/reject 时，只能一人获胜，且不得覆盖 EXECUTED。"""
    from app.crud.hitl_proposal import InvalidHitlTransitionError

    session_id, asset_id = await _make_context(db_session, test_user.id)
    proposal = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        asset_id=asset_id,
        payload={"message": "并发审批"},
        reason="并发 decide 回归",
        actor_user_id=test_user.id,
    )
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    ready = asyncio.Event()
    release = asyncio.Event()
    started = 0
    start_gate = asyncio.Lock()

    async def decide_and_commit(*, approve: bool) -> ProposalSafeSummary | Exception:
        """使用独立会话审批；在真正 decide 前同步，制造并发窗口。"""
        nonlocal started
        async with session_factory() as independent_session:
            async with start_gate:
                started += 1
                if started == 2:
                    ready.set()
            await release.wait()
            try:
                summary = await decide_proposal(
                    independent_session,
                    proposal_id=proposal.proposal_id,
                    approve=approve,
                    reviewed_by_user_id=test_user.id,
                )
                await independent_session.commit()
                return summary
            except Exception as exc:
                await independent_session.rollback()
                return exc

    approve_task = asyncio.create_task(decide_and_commit(approve=True))
    reject_task = asyncio.create_task(decide_and_commit(approve=False))
    await asyncio.wait_for(ready.wait(), timeout=1)
    release.set()
    approve_result, reject_result = await asyncio.gather(approve_task, reject_task)

    outcomes = [approve_result, reject_result]
    successes = [item for item in outcomes if isinstance(item, ProposalSafeSummary)]
    failures = [item for item in outcomes if isinstance(item, InvalidHitlTransitionError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert successes[0].status in {"APPROVED", "REJECTED"}

    stored = await hitl_proposal_crud.get(db_session, proposal.proposal_id)
    assert stored is not None
    assert stored.status == successes[0].status
    assert stored.status != "EXECUTED"

    if stored.status == "APPROVED":
        executed = await hitl_proposal_crud.mark_executed(db_session, proposal.proposal_id)
        await db_session.commit()
        assert executed.status == "EXECUTED"
        with pytest.raises(InvalidHitlTransitionError):
            await hitl_proposal_crud.decide(
                db_session,
                proposal.proposal_id,
                approve=False,
                reviewed_by_user_id=test_user.id,
            )
        final = await hitl_proposal_crud.get(db_session, proposal.proposal_id)
        assert final is not None
        assert final.status == "EXECUTED"


async def test_concurrent_resume_executes_successful_action_once(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个独立会话并发恢复时，执行器只能成功调用一次。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)
    proposal = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        asset_id=asset_id,
        payload={"message": "并发通知"},
        reason="并发幂等测试",
        actor_user_id=test_user.id,
    )
    await decide_proposal(
        db_session,
        proposal_id=proposal.proposal_id,
        approve=True,
        reviewed_by_user_id=test_user.id,
    )
    await db_session.commit()

    first_started = asyncio.Event()
    allow_execution = asyncio.Event()
    execute_count = 0

    class BlockingNotifyExecutor:
        """暂停首次执行，为第二个独立数据库会话制造并发窗口。"""

        async def execute(
            self,
            db: AsyncSession,
            *,
            proposal_id: int,
            payload: Mapping[str, object],
            actor_user_id: int | None,
        ) -> ExecutionResult:
            """记录调用次数，并在测试允许后返回成功。"""
            nonlocal execute_count
            execute_count += 1
            first_started.set()
            await allow_execution.wait()
            return ExecutionResult(ok=True, message="测试执行成功")

    monkeypatch.setattr(hitl, "_NOTIFY_EXECUTOR", BlockingNotifyExecutor())
    session_factory = async_sessionmaker(
        db_engine,
        expire_on_commit=False,
        autoflush=False,
    )

    async def resume_and_commit() -> ProposalSafeSummary:
        """使用独立会话恢复提案并提交事务。"""
        async with session_factory() as independent_session:
            summary = await resume_proposal(
                independent_session,
                proposal_id=proposal.proposal_id,
                actor_user_id=test_user.id,
            )
            await independent_session.commit()
            return summary

    first_task = asyncio.create_task(resume_and_commit())
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second_task = asyncio.create_task(resume_and_commit())
    await asyncio.sleep(0.05)
    allow_execution.set()
    first_summary, second_summary = await asyncio.gather(first_task, second_task)

    assert execute_count == 1
    assert first_summary == second_summary
    assert first_summary.status == "EXECUTED"


async def test_device_control_stub_failure_stays_approved(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """device_control stub 失败后保持 APPROVED 并发布失败事件。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)
    proposal = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset_id,
        payload={"command": "shutdown"},
        reason="维护窗口",
        actor_user_id=test_user.id,
    )
    await decide_proposal(
        db_session,
        proposal_id=proposal.proposal_id,
        approve=True,
        reviewed_by_user_id=test_user.id,
    )
    publisher = RecordingPublisher()

    summary = await resume_proposal(
        db_session,
        proposal_id=proposal.proposal_id,
        actor_user_id=test_user.id,
        publisher=publisher,
    )

    stored = await hitl_proposal_crud.get(db_session, proposal.proposal_id)
    assert stored is not None
    assert summary.status == "APPROVED"
    assert stored.executed_at is None
    assert [event[1] for event in publisher.events] == ["hitl_execution_failed"]


async def test_list_for_session_filters_status(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """会话提案列表可按状态过滤，并保持创建顺序。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    session_id, asset_id = await _make_context(db_session, test_user.id)
    first = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        asset_id=asset_id,
        payload={"message": "第一条"},
        reason="列表测试",
        actor_user_id=test_user.id,
    )
    second = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        asset_id=asset_id,
        payload={"message": "第二条"},
        reason="列表测试",
        actor_user_id=test_user.id,
    )
    await decide_proposal(
        db_session,
        proposal_id=second.proposal_id,
        approve=False,
        reviewed_by_user_id=test_user.id,
    )

    pending = await hitl_proposal_crud.list_for_session(
        db_session,
        session_id,
        status="PENDING",
    )

    assert [item.id for item in pending] == [first.proposal_id]
