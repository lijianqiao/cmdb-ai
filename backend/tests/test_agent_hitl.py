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
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent import hitl
from app.agent.executors import ExecutionResult
from app.agent.hitl import (
    HitlProposalRejectedError,
    HitlResumeError,
    ProposalSafeSummary,
    decide_proposal,
    gate_action,
    propose_action,
    resume_proposal,
)
from app.agent.hitl_gate import HitlGateHook, dispatch_through_hitl_gate
from app.agent.tool_dispatch import build_root_tool_dispatcher
from app.core.cmdb_credential import encrypt_credential_password
from app.core.config import settings
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import device_command_policy_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.hitl_proposal import HitlProposal
from app.models.user import User

pytestmark = pytest.mark.asyncio


def _hitl_session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """为门控钩子构造独立短会话工厂。"""
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)


def _make_hitl_gate(
    db_engine: AsyncEngine,
    *,
    session_id: int,
    actor_user_id: int,
    publisher: object | None = None,
) -> HitlGateHook:
    """创建绑定独立短会话工厂的门控钩子。"""
    return HitlGateHook(
        _hitl_session_factory(db_engine),
        session_id=session_id,
        actor_user_id=actor_user_id,
        publisher=publisher,
    )


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


async def _make_session_and_asset(db: AsyncSession, user_id: int) -> tuple[int, int]:
    """创建 device_query 测试用的会话与通用 CMDB 资产。"""
    return await _make_context(db, user_id)


async def _make_query_asset(
    db_session: AsyncSession,
    *,
    credential_type: str = "static",
    credential_username: str = "admin",
    credential_password_encrypted: str | None = "placeholder",
    vendor: str = "cisco_iosxe",
) -> int:
    """创建带厂商与凭据字段的交换机资产，供 device_query 策略测试使用。"""
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": "sw-hitl-01",
            "ip_address": "10.0.0.99",
            "vendor": vendor,
            "credential_type": credential_type,
            "credential_username": credential_username,
            "credential_password_encrypted": credential_password_encrypted,
        },
    )
    await db_session.flush()
    return asset.id


async def _proposal_count(db: AsyncSession) -> int:
    """返回当前事务中的 HITL 提案总数。"""
    result = await db.execute(select(func.count()).select_from(HitlProposal))
    return int(result.scalar_one())


async def _set_session_approval_mode(
    db_session: AsyncSession,
    session_id: int,
    approval_mode: str,
) -> None:
    """切换会话审批档位，供自动批准矩阵测试使用。"""
    session = await agent_session_crud.get(db_session, session_id)
    assert session is not None
    session.approval_mode = approval_mode
    await db_session.flush()


async def test_propose_merges_matching_asset_id_and_returns_safe_summary(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """冗余且匹配的 asset_id 应被剥离、校验并安全合并存储。"""
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
    assert set(asdict(summary)) == {
        "proposal_id",
        "action_type",
        "status",
        "reason",
        "asset_id",
        "result_excerpt",
        "last_error",
        "has_full_result",
    }
    assert [event[1] for event in publisher.events] == ["hitl_pending"]
    assert set(publisher.events[0][2]) == {
        "proposal_id",
        "action_type",
        "status",
        "reason",
        "asset_id",
        "result_excerpt",
        "last_error",
        "has_full_result",
    }


@pytest.mark.parametrize(
    ("action_type", "payload"),
    [
        ("unknown", {"message": "告警"}),
        ("notify", {"message": "告警", "secret": "不得接收"}),
        ("notify", {"message": 123}),
        ("device_control", {"command_name": 123}),
        ("device_control", {"command_name": "reboot", "interface_name": "eth0"}),  # reboot 不接受参数
        ("device_control", {"command_name": "port_disable"}),  # port_disable 缺 interface_name
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


async def test_gate_action_auto_approve_stays_approved_without_executor(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """assist 档位下 gate_action 自动批准应停留 APPROVED，且不调用执行器或 resume。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)
    await _set_session_approval_mode(db_session, session_id, "assist")

    async def fail_resume(*args: object, **kwargs: object) -> ProposalSafeSummary:
        raise AssertionError("resume_proposal 不应被 gate_action 调用")

    monkeypatch.setattr(hitl, "resume_proposal", fail_resume)

    summary = await gate_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        asset_id=asset_id,
        payload={"message": "自动通知"},
        reason="低风险告警",
        actor_user_id=test_user.id,
    )

    proposal = await hitl_proposal_crud.get(db_session, summary.proposal_id)
    assert proposal is not None
    assert summary.status == "APPROVED"
    assert proposal.reviewed_by_user_id == test_user.id
    assert proposal.executed_at is None


async def test_notify_auto_approve_executes_once_through_gate(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """assist 档位完整路径：门控 before 内统一执行，执行器只调一次。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)
    await _set_session_approval_mode(db_session, session_id, "assist")
    publisher = RecordingPublisher()
    gate = _make_hitl_gate(
        db_engine,
        session_id=session_id,
        actor_user_id=test_user.id,
        publisher=publisher,
    )
    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
        publisher=publisher,
        gate_hook=gate,
    )

    execute_count = 0

    async def counting_execute(*args: object, **kwargs: object) -> ExecutionResult:
        nonlocal execute_count
        execute_count += 1
        return ExecutionResult(ok=True, message="ok")

    monkeypatch.setattr(
        "app.agent.executors.NotifyExecutor.execute",
        counting_execute,
    )

    result = await dispatch_through_hitl_gate(
        gate,
        dispatch,
        "notify",
        {
            "asset_id": asset_id,
            "payload": {"message": "自动通知"},
            "reason": "低风险告警",
        },
    )

    proposal = (await hitl_proposal_crud.list_for_session(db_session, session_id))[0]
    assert result.control == "ok"
    assert execute_count == 1
    assert proposal is not None
    assert proposal.status == "EXECUTED"
    assert proposal.reviewed_by_user_id == test_user.id
    assert [event[1] for event in publisher.events] == ["hitl_pending", "hitl_resolved"]


async def test_propose_rejects_missing_session_without_insert(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """不存在的会话不得创建 HITL 提案。"""
    _, asset_id = await _make_context(db_session, test_user.id)

    with pytest.raises(HitlProposalRejectedError, match="会话不存在"):
        await propose_action(
            db_session,
            session_id=999_999,
            proposed_by_agent_id=None,
            action_type="notify",
            asset_id=asset_id,
            payload={"message": "告警"},
            reason="会话不存在",
            actor_user_id=test_user.id,
        )

    assert await _proposal_count(db_session) == 0


async def test_device_control_rejects_read_only_command_name(db_session: AsyncSession, test_user: User) -> None:
    """action_type=device_control 但传了只读命令名——两个工具的语义边界要在服务端强制。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)
    with pytest.raises(HitlProposalRejectedError, match="只读命令请使用 query_device_command"):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_control",
            asset_id=asset_id,
            payload={"command_name": "show_version"},
            reason="test",
            actor_user_id=test_user.id,
        )


async def test_device_query_rejects_state_changing_command_name(db_session: AsyncSession, test_user: User) -> None:
    """反过来，query_device_command 也不能被用来偷跑变更类命令。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)
    with pytest.raises(HitlProposalRejectedError, match="会改变设备状态的命令请使用 device_control"):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_query",
            asset_id=asset_id,
            payload={"command_name": "reboot"},
            reason="test",
            actor_user_id=test_user.id,
        )


async def test_decide_approve_does_not_resume_or_resolve(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """人工批准只改变状态，不应隐式执行或发布 resolved。"""
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
) -> None:
    """人工拒绝进入终态时应发布一次 resolved。"""
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
    reject_first: bool,
) -> None:
    """PENDING 与 REJECTED 均不允许绕过批准直接执行。"""
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
        await hitl_proposal_crud.claim_execution(db_session, proposal.proposal_id)
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
    proposal_id = proposal.proposal_id
    user_id = test_user.id

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

    blocking_executor = BlockingNotifyExecutor()

    async def patched_notify_execute(
        self: object,
        db: AsyncSession,
        *,
        proposal_id: int,
        payload: Mapping[str, object],
        actor_user_id: int | None,
    ) -> ExecutionResult:
        return await blocking_executor.execute(
            db,
            proposal_id=proposal_id,
            payload=payload,
            actor_user_id=actor_user_id,
        )

    monkeypatch.setattr(
        "app.agent.executors.NotifyExecutor.execute",
        patched_notify_execute,
    )
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
                proposal_id=proposal_id,
                actor_user_id=user_id,
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


async def test_device_control_connection_failure_reverts_to_approved(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未分类命令批准后，若连接都没建起来，回退 APPROVED 可重试（绝不伪造 EXECUTED）。

    连接失败说明命令确定没下发、设备状态没被改动，因此不需要 UNKNOWN 的人工核实；
    但同样绝不能被当成执行成功。
    """
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_query_asset(
        db_session,
        vendor="linux",
        credential_type="static",
        credential_password_encrypted=ciphertext,
    )
    proposal = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset_id,
        payload={"command_name": "shutdown"},
        reason="维护窗口",
        actor_user_id=test_user.id,
    )
    assert proposal.status == "PENDING"
    await decide_proposal(
        db_session,
        proposal_id=proposal.proposal_id,
        approve=True,
        reviewed_by_user_id=test_user.id,
    )
    publisher = RecordingPublisher()

    from unittest.mock import patch

    with patch("app.agent.executors._open_netmiko_connection", side_effect=ConnectionError("unreachable")):
        summary = await resume_proposal(
            db_session,
            proposal_id=proposal.proposal_id,
            actor_user_id=test_user.id,
            publisher=publisher,
        )

    stored = await hitl_proposal_crud.get(db_session, proposal.proposal_id)
    assert stored is not None
    assert summary.status == "APPROVED"
    assert stored.status_reason == "dispatch_failed_before_send"
    assert stored.executed_at is None
    assert stored.execution_started_at is None
    assert "ConnectionError" in str(stored.action_payload.get("last_error"))
    assert [event[1] for event in publisher.events] == ["hitl_execution_failed"]


async def test_resume_retry_after_unknown_requires_allow_retry(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UNKNOWN 直接 retry 被拒绝；人工 allow_retry 后才能再次执行成功。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_query_asset(
        db_session,
        vendor="linux",
        credential_type="static",
        credential_password_encrypted=ciphertext,
    )
    proposal = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_query",
        asset_id=asset_id,
        payload={"command_name": "show_version"},
        reason="排查",
        actor_user_id=test_user.id,
    )
    await decide_proposal(
        db_session,
        proposal_id=proposal.proposal_id,
        approve=True,
        reviewed_by_user_id=test_user.id,
    )
    proposal_id = proposal.proposal_id
    user_id = test_user.id

    from unittest.mock import MagicMock, patch

    # 连接已建立、命令下发途中断开：无法确定命令是否已生效，必须走 UNKNOWN 人工核实。
    broken_connection = MagicMock()
    broken_connection.send_command = MagicMock(side_effect=ConnectionError("dropped mid-command"))
    with patch("app.agent.executors._open_netmiko_connection", return_value=broken_connection):
        failed = await resume_proposal(db_session, proposal_id=proposal_id, actor_user_id=user_id)
    assert failed.status == "UNKNOWN"

    with pytest.raises(HitlResumeError):
        await resume_proposal(db_session, proposal_id=proposal_id, actor_user_id=user_id)

    await hitl_proposal_crud.resolve_unknown(
        db_session,
        proposal_id,
        resolution="allow_retry",
        resolved_by_user_id=user_id,
    )
    await db_session.commit()

    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value="Linux host info")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        retried = await resume_proposal(db_session, proposal_id=proposal_id, actor_user_id=user_id)

    stored = await hitl_proposal_crud.get(db_session, proposal_id)
    assert stored is not None
    assert retried.status == "EXECUTED"
    assert stored.action_payload.get("last_result_excerpt") == "Linux host info"


async def test_unclassified_device_control_stays_pending(
    db_session: AsyncSession, test_user: User
) -> None:
    """未分类的变更类命令在 ask 档位下必须停在 PENDING（跟 device_query 完全对称）。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(
        db_session,
        vendor="cisco_iosxe",
        credential_type="static",
        credential_password_encrypted="placeholder",
    )
    summary = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset_id,
        payload={"command_name": "reboot"},
        reason="故障恢复",
        actor_user_id=test_user.id,
    )
    assert summary.status == "PENDING"


async def test_whitelisted_device_control_auto_executes_with_static_credential(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """白名单 + 静态凭据：跟 device_query 一样一次调用直接 EXECUTED。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_query_asset(
        db_session,
        vendor="cisco_iosxe",
        credential_password_encrypted=ciphertext,
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "reboot",
            "decision": "whitelist",
        },
    )
    await db_session.commit()
    await _set_session_approval_mode(db_session, session_id, "assist")

    from unittest.mock import MagicMock, patch

    fake_connection = MagicMock()
    fake_connection.send_command_timing = MagicMock(return_value="rebooting")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        gate = _make_hitl_gate(db_engine, session_id=session_id, actor_user_id=test_user.id)
        dispatch = build_root_tool_dispatcher(
            db_session,
            session_id=session_id,
            actor_user_id=test_user.id,
            gate_hook=gate,
        )
        await dispatch_through_hitl_gate(
            gate,
            dispatch,
            "device_control",
            {"asset_id": asset_id, "command_name": "reboot", "reason": "故障恢复"},
        )

    proposal = await hitl_proposal_crud.list_for_session(db_session, session_id)
    assert len(proposal) == 1
    assert proposal[0].status == "EXECUTED"


async def test_dynamic_credential_device_control_never_auto_executes(db_session: AsyncSession, test_user: User) -> None:
    """跟 device_query 的既有规则完全对称：动态凭据永远至少过一次人工。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(
        db_session,
        credential_type="dynamic",
        credential_password_encrypted=None,
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "reboot",
            "decision": "whitelist",
        },
    )
    await db_session.commit()

    summary = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset_id,
        payload={"command_name": "reboot"},
        reason="故障恢复",
        actor_user_id=test_user.id,
    )

    assert summary.status == "PENDING"


async def test_device_control_reboot_rejects_interface_name_with_credentialed_asset(
    db_session: AsyncSession, test_user: User
) -> None:
    """reboot 带 interface_name 时，凭据/厂商通过后应明确拒绝多余参数。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session)

    with pytest.raises(HitlProposalRejectedError, match="不接受 interface_name"):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_control",
            asset_id=asset_id,
            payload={"command_name": "reboot", "interface_name": "GigabitEthernet0/1"},
            reason="reboot 不接受接口名",
            actor_user_id=test_user.id,
        )

    assert await _proposal_count(db_session) == 0


async def test_device_control_port_disable_rejects_missing_interface_name(
    db_session: AsyncSession, test_user: User
) -> None:
    """port_disable 缺 interface_name 时，凭据/厂商通过后应要求合法接口名。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session)

    with pytest.raises(HitlProposalRejectedError, match="合法的接口名"):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_control",
            asset_id=asset_id,
            payload={"command_name": "port_disable"},
            reason="port_disable 缺接口名",
            actor_user_id=test_user.id,
        )

    assert await _proposal_count(db_session) == 0


async def test_device_control_port_disable_rejects_illegal_interface_name(
    db_session: AsyncSession, test_user: User
) -> None:
    """port_disable 接口名含非法字符时，凭据/厂商通过后应拒绝。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session)

    with pytest.raises(HitlProposalRejectedError, match="合法的接口名"):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_control",
            asset_id=asset_id,
            payload={"command_name": "port_disable", "interface_name": "eth0; rm -rf /"},
            reason="非法接口名",
            actor_user_id=test_user.id,
        )

    assert await _proposal_count(db_session) == 0


async def test_list_for_session_filters_status(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """会话提案列表可按状态过滤，并保持创建顺序。"""
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


async def test_propose_device_query_creates_pending_proposal(db_session: AsyncSession, test_user: User) -> None:
    """device_query 在资产具备凭据与厂商时应通过校验并创建 PENDING 提案。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session)

    summary = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_query",
        asset_id=asset_id,
        payload={"command_name": "show_version"},
        reason="排查交换机异常",
        actor_user_id=test_user.id,
    )

    assert summary.status == "PENDING"
    assert summary.action_type == "device_query"


async def test_propose_device_query_rejects_extra_payload_fields(db_session: AsyncSession, test_user: User) -> None:
    """device_query 载荷含多余字段时应拒绝。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)

    with pytest.raises(HitlProposalRejectedError):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_query",
            asset_id=asset_id,
            payload={"command_name": "show_version", "extra_field": "nope"},
            reason="排查交换机异常",
            actor_user_id=test_user.id,
        )


async def test_proposal_safe_summary_includes_result_excerpt_field() -> None:
    """ProposalSafeSummary 应包含 result_excerpt 字段。"""
    from dataclasses import fields

    field_names = {f.name for f in fields(ProposalSafeSummary)}
    assert "result_excerpt" in field_names


async def test_device_query_rejects_when_asset_has_no_credential(db_session: AsyncSession, test_user: User) -> None:
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session, credential_type="none", credential_password_encrypted=None)

    with pytest.raises(HitlProposalRejectedError):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_query",
            asset_id=asset_id,
            payload={"command_name": "show_version"},
            reason="test",
            actor_user_id=test_user.id,
        )


async def test_device_query_rejects_unknown_command_name(db_session: AsyncSession, test_user: User) -> None:
    """命令名根本不在目录里，报错要明确说"未知命令名"，不能跟厂商不支持混为一谈。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session)

    with pytest.raises(HitlProposalRejectedError) as exc_info:
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_query",
            asset_id=asset_id,
            payload={"command_name": "unknown_command"},
            reason="test",
            actor_user_id=test_user.id,
        )

    assert "未知命令名" in str(exc_info.value)


async def test_device_query_rejects_command_unsupported_by_vendor(db_session: AsyncSession, test_user: User) -> None:
    """命令存在，但目录里没给这个厂商登记模板——报错要明确说"厂商不支持"，不是"未知命令名"。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    # show_running_config 命令目录里没有 linux 的模板（见 device_commands.py）
    asset_id = await _make_query_asset(db_session, vendor="linux")

    with pytest.raises(HitlProposalRejectedError) as exc_info:
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_query",
            asset_id=asset_id,
            payload={"command_name": "show_running_config"},
            reason="test",
            actor_user_id=test_user.id,
        )

    assert "该设备厂商不支持这个命令" in str(exc_info.value)
    assert "未知命令名" not in str(exc_info.value)
    # 拒绝原因必须可行动：列出该厂商真正支持的命令供模型纠正。
    assert "show_version" in str(exc_info.value)


async def test_device_query_blacklist_rejects_without_creating_proposal(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session)
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_running_config",
            "decision": "blacklist",
        },
    )
    await db_session.commit()

    with pytest.raises(HitlProposalRejectedError):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_query",
            asset_id=asset_id,
            payload={"command_name": "show_running_config"},
            reason="test",
            actor_user_id=test_user.id,
        )

    proposals = await hitl_proposal_crud.list_for_session(db_session, session_id)
    assert proposals == []


async def test_device_query_whitelist_pends_in_ask_mode_with_static_credential(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ask 档位下白名单 + 静态凭据的 device_query 应停在 PENDING。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_query_asset(db_session, credential_password_encrypted=ciphertext)
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )
    await db_session.commit()

    summary = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_query",
        asset_id=asset_id,
        payload={"command_name": "show_version"},
        reason="test",
        actor_user_id=test_user.id,
    )

    assert summary.status == "PENDING"


async def test_device_query_whitelist_auto_executes_for_static_credential(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """assist 档位下白名单 + 静态凭据的 device_query 应一次调用直接 EXECUTED。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_query_asset(db_session, credential_password_encrypted=ciphertext)
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )
    await db_session.commit()
    await _set_session_approval_mode(db_session, session_id, "assist")

    from unittest.mock import MagicMock, patch

    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value="fake output")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        gate = _make_hitl_gate(db_engine, session_id=session_id, actor_user_id=test_user.id)
        dispatch = build_root_tool_dispatcher(
            db_session,
            session_id=session_id,
            actor_user_id=test_user.id,
            gate_hook=gate,
        )
        await dispatch_through_hitl_gate(
            gate,
            dispatch,
            "query_device_command",
            {"asset_id": asset_id, "command_name": "show_version", "reason": "test"},
        )

    proposals = await hitl_proposal_crud.list_for_session(db_session, session_id)
    assert len(proposals) == 1
    assert proposals[0].status == "EXECUTED"
    assert proposals[0].action_payload.get("last_result_excerpt") is not None
    assert "fake output" in str(proposals[0].action_payload.get("last_result_excerpt"))


async def test_device_query_whitelist_still_pends_for_dynamic_credential(
    db_session: AsyncSession, test_user: User
) -> None:
    """动态凭据即使白名单且 full 档位也必须至少过一次人工。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session, credential_type="dynamic", credential_password_encrypted=None)
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )
    await db_session.commit()
    await _set_session_approval_mode(db_session, session_id, "full")

    summary = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_query",
        asset_id=asset_id,
        payload={"command_name": "show_version"},
        reason="test",
        actor_user_id=test_user.id,
    )

    assert summary.status == "PENDING"


@pytest.mark.parametrize("approval_mode", ["ask", "assist"])
async def test_device_query_unclassified_command_pends(
    db_session: AsyncSession, test_user: User, approval_mode: str
) -> None:
    """ask/assist 档位下未分类 device_query 应停在 PENDING。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session)
    await _set_session_approval_mode(db_session, session_id, approval_mode)

    summary = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_query",
        asset_id=asset_id,
        payload={"command_name": "show_version"},
        reason="test",
        actor_user_id=test_user.id,
    )

    assert summary.status == "PENDING"


async def test_device_query_unclassified_auto_executes_in_full_mode(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """full 档位下未分类 + 静态凭据的 device_query 应一次调用直接 EXECUTED。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_query_asset(db_session, credential_password_encrypted=ciphertext)
    await _set_session_approval_mode(db_session, session_id, "full")

    from unittest.mock import MagicMock, patch

    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value="full mode output")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        gate = _make_hitl_gate(db_engine, session_id=session_id, actor_user_id=test_user.id)
        dispatch = build_root_tool_dispatcher(
            db_session,
            session_id=session_id,
            actor_user_id=test_user.id,
            gate_hook=gate,
        )
        await dispatch_through_hitl_gate(
            gate,
            dispatch,
            "query_device_command",
            {"asset_id": asset_id, "command_name": "show_version", "reason": "test"},
        )

    proposals = await hitl_proposal_crud.list_for_session(db_session, session_id)
    assert len(proposals) == 1
    assert proposals[0].status == "EXECUTED"


@pytest.mark.parametrize("approval_mode", ["ask", "assist", "full"])
async def test_device_query_blacklist_rejects_in_all_approval_modes(
    db_session: AsyncSession, test_user: User, approval_mode: str
) -> None:
    """三档审批模式下黑名单命令均不得创建提案。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session)
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_running_config",
            "decision": "blacklist",
        },
    )
    await db_session.commit()
    await _set_session_approval_mode(db_session, session_id, approval_mode)

    with pytest.raises(HitlProposalRejectedError):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_query",
            asset_id=asset_id,
            payload={"command_name": "show_running_config"},
            reason="test",
            actor_user_id=test_user.id,
        )

    assert await _proposal_count(db_session) == 0


async def test_resume_proposal_passes_dynamic_password_to_executor(db_session: AsyncSession, test_user: User) -> None:
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session, credential_type="dynamic", credential_password_encrypted=None)
    summary = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_query",
        asset_id=asset_id,
        payload={"command_name": "show_version"},
        reason="test",
        actor_user_id=test_user.id,
    )
    await decide_proposal(
        db_session,
        proposal_id=summary.proposal_id,
        approve=True,
        reviewed_by_user_id=test_user.id,
    )
    await db_session.commit()

    from unittest.mock import MagicMock, patch

    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value="otp output")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        resumed = await resume_proposal(
            db_session,
            proposal_id=summary.proposal_id,
            actor_user_id=test_user.id,
            dynamic_password="one-time-pass",
        )

    assert resumed.status == "EXECUTED"
