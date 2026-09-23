"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl_execution.py
@DateTime: 2026-08-14
@Docs: HITL 独立执行服务：策略复检、持久化认领、外部执行与 UNKNOWN 恢复。

实现流程：
1. execute_approved_proposal 先经 _preflight_and_claim 在同一短事务内复检授权与策略并认领 EXECUTING；
   授权按触发人此刻的账号状态与权限现查（人工路径看审批权限，档位自动批准看自动执行权限），
   不通过则不认领、不下发，提案保持 APPROVED 并写明原因。
2. 认领提交后 _execute_prepared 调用注入或默认执行器；执行器内可观测已提交的 EXECUTING 状态。
3. 执行成功后在同一收尾事务 mark_executed、保存安全预览，并仅为查询动作保存完整正文；执行失败或异常统一 mark_unknown(dispatch_outcome_unknown)。
4. reconcile_executing_proposals 供启动恢复，将遗留 EXECUTING 批量转为 UNKNOWN。
5. 预检失败（命令不存在、动态凭据缺失）不认领，提案保持 APPROVED。
"""

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import make_transient

from app.agent.device_commands import (
    CommandArguments,
    command_supports_vendor,
    command_type_of,
)
from app.agent.executors import (
    DeviceQueryExecutor,
    ExecutionResult,
    NotifyExecutor,
    device_read_timeout_seconds,
)
from app.agent.hitl import (
    ActionType,
    HitlEventPublisher,
    HitlResumeError,
    ProposalSafeSummary,
    _publish,
    _summary,
    payload_command_arguments,
)
from app.agent.permissions import HITL_APPROVE, load_permission_context
from app.core.config import settings
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import device_command_policy_crud
from app.crud.hitl_execution_result import hitl_execution_result_crud
from app.crud.hitl_proposal import InvalidHitlTransitionError, hitl_proposal_crud
from app.models.cmdb_asset import CmdbAsset
from app.models.hitl_proposal import HitlProposal
from app.utils.audit import log_audit

logger = logging.getLogger(__name__)

_OUTPUT_PREVIEW_LIMIT = 4000
# 并发执行轮询：指数退避而不是固定 50ms 死等。原实现是 200 次 × 50ms，
# 最坏打 200 次数据库往返却只覆盖 10 秒——比设备命令的读超时还短，
# 该等到的没等到，不该打的往返全打了。
_POLL_INITIAL_DELAY_SECONDS = 0.05
_POLL_MAX_DELAY_SECONDS = 2.0


def build_result_preview(text: str, *, limit: int = _OUTPUT_PREVIEW_LIMIT) -> str:
    """构造可进入安全摘要的设备输出预览，完整正文只保存至专用结果表。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "…(截断)"


class NotifyExecutorProtocol(Protocol):
    """通知类 HITL 动作的外部执行协议。"""

    async def execute(
        self,
        db: AsyncSession,
        *,
        proposal_id: int,
        payload: Mapping[str, object],
        actor_user_id: int | None,
    ) -> ExecutionResult:
        """执行已认领的通知提案。"""
        raise NotImplementedError


class DeviceExecutorProtocol(Protocol):
    """设备诊断/管控类 HITL 动作的外部执行协议。"""

    async def execute(
        self,
        db: AsyncSession,
        *,
        asset: CmdbAsset,
        command_name: str,
        dynamic_password: str | None,
        arguments: CommandArguments | None = None,
    ) -> ExecutionResult:
        """执行已认领的设备命令提案。"""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    """认领并提交 EXECUTING 后的执行上下文快照。"""

    proposal_id: int
    session_id: int
    action_type: ActionType
    payload: dict[str, object]
    asset: CmdbAsset | None = None


async def _summary_with_persisted_result(
    db: AsyncSession,
    proposal: HitlProposal,
) -> ProposalSafeSummary:
    """仅由专用结果表的持久化记录派生完整结果存在标志。"""
    result = await hitl_execution_result_crud.get_by_proposal(db, proposal.id)
    return _summary(proposal, has_full_result=result is not None)


def _detach_asset(asset: CmdbAsset) -> CmdbAsset:
    """从会话中分离资产副本，供后续独立事务使用。"""
    make_transient(asset)
    return asset


def _poll_wait_seconds(proposal: HitlProposal) -> float:
    """等别人跑完的窗口：按这条命令的读超时档位取，长命令不能按 60 秒判超时。"""
    command_name = proposal.action_payload.get("command_name")
    if isinstance(command_name, str):
        return device_read_timeout_seconds(command_name)
    return settings.DEVICE_COMMAND_READ_TIMEOUT_SECONDS


async def _poll_executing_terminal(
    db: AsyncSession,
    proposal_id: int,
    *,
    wait_seconds: float,
) -> HitlProposal | ProposalSafeSummary:
    """轮询 EXECUTING 直至终态；APPROVED 时返回提案行供重试认领。

    Args:
        db: 当前短事务会话。
        proposal_id: 待轮询提案 ID。
        wait_seconds: 等待窗口秒数，由调用方按命令的读超时档位算出。

    Returns:
        EXECUTED 时返回安全摘要；APPROVED 时返回提案行；其它终态抛错。

    Raises:
        HitlResumeError: 提案不存在、UNKNOWN 或等待超时。
    """
    # 等待窗口对齐设备命令读超时：另一个执行者跑的可能正是一条慢命令。
    deadline = time.monotonic() + wait_seconds
    delay = _POLL_INITIAL_DELAY_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(delay)
        delay = min(delay * 2, _POLL_MAX_DELAY_SECONDS)
        db.expire_all()
        refreshed = await hitl_proposal_crud.get(db, proposal_id)
        if refreshed is None:
            raise HitlResumeError(f"HITL 提案不存在：{proposal_id}")
        if refreshed.status == "EXECUTED":
            return await _summary_with_persisted_result(db, refreshed)
        if refreshed.status == "APPROVED":
            return refreshed
        if refreshed.status == "UNKNOWN":
            raise HitlResumeError("状态 UNKNOWN 的 HITL 提案不可恢复执行")
        if refreshed.status != "EXECUTING":
            raise HitlResumeError(f"状态 {refreshed.status} 的 HITL 提案不可恢复执行")
    raise HitlResumeError("等待并发执行完成超时")


async def _handle_claim_conflict(
    db: AsyncSession,
    proposal_id: int,
) -> HitlProposal | ProposalSafeSummary:
    """认领 CAS 失败后刷新状态并分流到终态摘要或轮询。

    Args:
        db: 当前短事务会话。
        proposal_id: 提案 ID。

    Returns:
        EXECUTED 时返回安全摘要；EXECUTING 时轮询；APPROVED 时返回提案行。

    Raises:
        HitlResumeError: UNKNOWN 或其它不可恢复状态。
    """
    db.expire_all()
    refreshed = await hitl_proposal_crud.get(db, proposal_id)
    if refreshed is None:
        raise HitlResumeError(f"HITL 提案不存在：{proposal_id}")
    if refreshed.status == "EXECUTED":
        return await _summary_with_persisted_result(db, refreshed)
    if refreshed.status == "UNKNOWN":
        raise HitlResumeError("状态 UNKNOWN 的 HITL 提案不可恢复执行")
    if refreshed.status == "EXECUTING":
        return await _poll_executing_terminal(
            db, proposal_id, wait_seconds=_poll_wait_seconds(refreshed)
        )
    if refreshed.status == "APPROVED":
        return refreshed
    raise HitlResumeError(f"状态 {refreshed.status} 的 HITL 提案不可恢复执行")


async def _authorization_denial(
    db: AsyncSession,
    actor_user_id: int | None,
    required_permission: str,
) -> str | None:
    """执行前复核触发人的当前授权。

    批准和真正下发之间有时间差（S6 异步执行后会更长）：这段时间里账号可能被停用、
    权限可能被撤销。撤销后还没下发的操作必须停下；已经下发的命令没法撤回，
    那一侧照常记录真实结果或 UNKNOWN，不在这里处理。

    Returns:
        拒绝原因（会写进 last_error 给界面和模型看）；None 表示放行。
    """
    context = await load_permission_context(db, actor_user_id)
    if context is None:
        return "执行前复核未通过：触发执行的账号不存在或已停用"
    if not context.has(required_permission):
        return f"执行前复核未通过：当前账号已无权执行（需要权限：{required_permission}）"
    return None


async def _preflight_and_claim(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    proposal_id: int,
    dynamic_password: str | None,
    publisher: HitlEventPublisher | None,
    actor_user_id: int | None,
    required_permission: str,
) -> PreparedExecution | ProposalSafeSummary:
    """在同一短事务内完成复检、策略拒绝或认领 EXECUTING 并提交。

    Args:
        session_factory: 独立短会话工厂。
        proposal_id: 待执行提案 ID。
        dynamic_password: 动态凭据明文，不落库。
        publisher: 可选安全事件发布器。
        actor_user_id: 触发执行的账号。
        required_permission: 触发人此刻必须持有的权限（人工 / 自动执行各不相同）。

    Returns:
        认领成功返回 PreparedExecution；否则返回当前安全摘要。

    Raises:
        HitlResumeError: 提案不存在或状态不可执行时。
    """
    async with session_factory() as db:
        proposal = await hitl_proposal_crud.get(db, proposal_id)
        if proposal is None:
            raise HitlResumeError(f"HITL 提案不存在：{proposal_id}")
        if proposal.status == "EXECUTED":
            return await _summary_with_persisted_result(db, proposal)
        if proposal.status == "EXECUTING":
            polled = await _poll_executing_terminal(
                db, proposal_id, wait_seconds=_poll_wait_seconds(proposal)
            )
            if isinstance(polled, ProposalSafeSummary):
                return polled
            proposal = polled
        if proposal.status != "APPROVED":
            raise HitlResumeError(f"状态 {proposal.status} 的 HITL 提案不可恢复执行")

        denial = await _authorization_denial(db, actor_user_id, required_permission)
        if denial is not None:
            _store_last_error(proposal, denial)
            await db.commit()
            return _summary(proposal)

        detached_asset: CmdbAsset | None = None
        action_type = proposal.action_type

        if action_type in ("device_query", "device_control"):
            raw_asset_id = proposal.action_payload.get("asset_id")
            if not isinstance(raw_asset_id, int) or isinstance(raw_asset_id, bool):
                return _summary(proposal)

            asset = await cmdb_asset_crud.get(db, raw_asset_id)
            if asset is None:
                return _summary(proposal)

            raw_command_name = proposal.action_payload.get("command_name")
            if not isinstance(raw_command_name, str) or not raw_command_name:
                return _summary(proposal)
            command_name = raw_command_name

            command_type = command_type_of(command_name)
            if command_type is None:
                return _summary(proposal)
            if action_type == "device_query" and command_type != "read_only":
                return _summary(proposal)
            if action_type == "device_control" and command_type != "state_changing":
                return _summary(proposal)

            if asset.credential_type == "none":
                return _summary(proposal)
            if not asset.vendor:
                return _summary(proposal)
            if not command_supports_vendor(command_name, asset.vendor):
                return _summary(proposal)

            try:
                payload_command_arguments(command_name, proposal.action_payload)
            except ValueError:
                # 参数缺失或不合法：不认领、不下发，提案留在 APPROVED 等人处理。
                return _summary(proposal)

            policy_decision = await device_command_policy_crud.resolve_policy(
                db,
                asset_id=asset.id,
                asset_type=asset.asset_type,
                command_name=command_name,
            )
            if policy_decision == "blacklist":
                rejected = await hitl_proposal_crud.reject_for_policy(db, proposal_id)
                await db.commit()
                await _publish(publisher, proposal=rejected, event_type="hitl_resolved")
                return _summary(rejected)

            if asset.credential_type == "dynamic":
                if not isinstance(dynamic_password, str) or not dynamic_password.strip():
                    return _summary(proposal)

            detached_asset = _detach_asset(asset)

        try:
            claimed = await hitl_proposal_crud.claim_execution(db, proposal_id)
        except InvalidHitlTransitionError:
            conflict = await _handle_claim_conflict(db, proposal_id)
            if isinstance(conflict, ProposalSafeSummary):
                return conflict
            proposal = conflict
            if proposal.status != "APPROVED":
                raise HitlResumeError(
                    f"状态 {proposal.status} 的 HITL 提案不可恢复执行"
                ) from None
            try:
                claimed = await hitl_proposal_crud.claim_execution(db, proposal_id)
            except InvalidHitlTransitionError:
                retry_conflict = await _handle_claim_conflict(db, proposal_id)
                if isinstance(retry_conflict, ProposalSafeSummary):
                    return retry_conflict
                raise HitlResumeError(
                    f"状态 {retry_conflict.status} 的 HITL 提案不可恢复执行"
                ) from None
        await db.commit()

        copied_payload = dict(claimed.action_payload)
        if action_type not in ("notify", "device_control", "device_query"):
            raise HitlResumeError(f"不支持的 HITL 动作类型：{action_type}")

        validated_action_type = cast(ActionType, action_type)

        return PreparedExecution(
            proposal_id=claimed.id,
            session_id=claimed.session_id,
            action_type=validated_action_type,
            payload=copied_payload,
            asset=detached_asset,
        )


async def _execute_prepared(
    db: AsyncSession,
    prepared: PreparedExecution,
    *,
    actor_user_id: int | None,
    dynamic_password: str | None,
    notify_executor: NotifyExecutorProtocol,
    device_executor: DeviceExecutorProtocol,
) -> ExecutionResult:
    """按动作类型调用对应执行器。

    Args:
        db: 执行阶段事务会话。
        prepared: 已认领的执行快照。
        actor_user_id: 触发执行的用户 ID。
        dynamic_password: 动态凭据明文。
        notify_executor: 通知执行器。
        device_executor: 设备执行器。

    Returns:
        执行器返回结果。
    """
    if prepared.action_type == "notify":
        return await notify_executor.execute(
            db,
            proposal_id=prepared.proposal_id,
            payload=prepared.payload,
            actor_user_id=actor_user_id,
        )

    if prepared.action_type in ("device_query", "device_control"):
        if prepared.asset is None:
            return ExecutionResult(ok=False, message="资产不存在")
        command_name = str(prepared.payload.get("command_name"))
        return await device_executor.execute(
            db,
            asset=prepared.asset,
            command_name=command_name,
            dynamic_password=dynamic_password,
            arguments=payload_command_arguments(command_name, prepared.payload),
        )

    raise HitlResumeError(f"不支持的 HITL 动作类型：{prepared.action_type}")


def _describe_failure(result: ExecutionResult | None) -> str:
    """把执行器结果压成一句可展示的失败原因（含异常类名，便于直接定位）。"""
    if result is None:
        return "执行器无返回"
    error_class = result.detail.get("error_class")
    if isinstance(error_class, str) and error_class:
        return f"{result.message}（{error_class}）"
    return result.message


def _store_last_error(proposal: HitlProposal, last_error: str | None) -> None:
    """把失败原因写进 action_payload["last_error"]，供安全摘要与前端展示。

    这里只存分类信息（执行器的 message 或异常类名），不存原始异常文本。
    """
    if not last_error:
        return
    updated_payload = dict(proposal.action_payload)
    updated_payload["last_error"] = last_error
    if updated_payload != proposal.action_payload:
        proposal.action_payload = updated_payload


async def _mark_execution_unknown(
    session_factory: async_sessionmaker[AsyncSession],
    proposal_id: int,
    publisher: HitlEventPublisher | None,
    actor_user_id: int | None = None,
    last_error: str | None = None,
    actor_ip: str = "",
) -> ProposalSafeSummary:
    """将 EXECUTING 提案标记为 UNKNOWN 并发布安全事件。

    Args:
        session_factory: 独立短会话工厂。
        proposal_id: 提案 ID。
        publisher: 可选安全事件发布器。
        actor_user_id: 触发执行的用户 ID，用于审计。
        last_error: 失败分类信息，写入 action_payload 供审批卡片展示。

    Returns:
        UNKNOWN 状态的安全摘要。
    """
    async with session_factory() as db:
        unknown = await hitl_proposal_crud.mark_unknown(
            db,
            proposal_id,
            reason="dispatch_outcome_unknown",
        )
        _store_last_error(unknown, last_error)
        await db.flush()
        await log_audit(
            db,
            actor_user_id,
            "hitl_execution_unknown",
            target=f"hitl_proposal:{unknown.id}",
            # 审计 detail 刻意不带异常文本（见 test_unknown_execution_writes_audit）；
            # 失败原因走 action_payload["last_error"] 与服务端日志两条通道。
            detail=f"动作类型：{unknown.action_type}",
            ip=actor_ip,
        )
        await db.commit()

    await _publish(publisher, proposal=unknown, event_type="hitl_execution_failed")
    return _summary(unknown)


async def _mark_execution_unexecuted(
    session_factory: async_sessionmaker[AsyncSession],
    proposal_id: int,
    publisher: HitlEventPublisher | None,
    actor_user_id: int | None = None,
    last_error: str | None = None,
    actor_ip: str = "",
    reason: str = "dispatch_failed_before_send",
) -> ProposalSafeSummary:
    """不需要人工核实设备状态的失败：把 EXECUTING 回退成 APPROVED 并发布安全事件。

    与 _mark_execution_unknown 的区别是不必先人工核实设备实际状态：要么执行器确认
    没碰到设备（连接都没建起来），要么失败的是没有副作用的只读命令，重跑一次是安全的。

    Args:
        session_factory: 独立短会话工厂。
        proposal_id: 提案 ID。
        publisher: 可选安全事件发布器。
        actor_user_id: 触发执行的用户 ID，用于审计。
        last_error: 失败分类信息，写入 action_payload 供审批卡片展示。
        reason: 状态机允许的回退原因码（见 crud.hitl_proposal.revert_unexecuted）。

    Returns:
        回退到 APPROVED 后的安全摘要。
    """
    async with session_factory() as db:
        reverted = await hitl_proposal_crud.revert_unexecuted(
            db,
            proposal_id,
            reason=reason,
        )
        _store_last_error(reverted, last_error)
        await db.flush()
        await log_audit(
            db,
            actor_user_id,
            "hitl_execution_not_dispatched",
            target=f"hitl_proposal:{reverted.id}",
            # 与 UNKNOWN 路径一致：审计 detail 不带异常文本，只带动作类型和原因码。
            detail=f"动作类型：{reverted.action_type}；原因：{reason}",
            ip=actor_ip,
        )
        await db.commit()

    await _publish(publisher, proposal=reverted, event_type="hitl_execution_failed")
    return _summary(reverted)


async def _publish_execution_summary(
    publisher: HitlEventPublisher | None,
    proposal: HitlProposal,
    *,
    has_full_result: bool,
) -> None:
    """发布执行成功后的安全摘要事件。"""
    await _publish(
        publisher,
        proposal=proposal,
        event_type="hitl_resolved",
        has_full_result=has_full_result,
    )


async def execute_approved_proposal(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    proposal_id: int,
    actor_user_id: int | None,
    publisher: HitlEventPublisher | None = None,
    dynamic_password: str | None = None,
    notify_executor: NotifyExecutorProtocol | None = None,
    device_executor: DeviceExecutorProtocol | None = None,
    actor_ip: str = "",
    required_permission: str = HITL_APPROVE,
) -> ProposalSafeSummary:
    """执行已批准提案：复检授权与策略、认领 EXECUTING、调用外部执行器并完成状态迁移。

    Args:
        session_factory: 独立短会话工厂。
        proposal_id: 待执行提案 ID。
        actor_user_id: 触发执行的用户 ID。
        publisher: 可选安全事件发布器。
        dynamic_password: 动态凭据明文，不落库。
        notify_executor: 可选通知执行器。
        device_executor: 可选设备执行器。
        actor_ip: 触发来源 IP，写进审计。
        required_permission: 触发人此刻必须持有的权限。默认是人工审批权限（更严的一侧，
            调用方忘了传也不会放宽）；档位自动批准的路径必须显式传 agent:auto_execute。

    Returns:
        执行完成或预检/UNKNOWN 后的安全摘要。

    Raises:
        HitlResumeError: 提案状态不可执行时。
        asyncio.CancelledError: 取消异常在持久化 UNKNOWN 后继续抛出。
    """
    prepared = await _preflight_and_claim(
        session_factory=session_factory,
        proposal_id=proposal_id,
        dynamic_password=dynamic_password,
        publisher=publisher,
        actor_user_id=actor_user_id,
        required_permission=required_permission,
    )
    if isinstance(prepared, ProposalSafeSummary):
        return prepared

    result: ExecutionResult | None = None
    try:
        async with session_factory() as execution_db:
            result = await _execute_prepared(
                execution_db,
                prepared,
                actor_user_id=actor_user_id,
                dynamic_password=dynamic_password,
                notify_executor=notify_executor or NotifyExecutor(),
                device_executor=device_executor or DeviceQueryExecutor(),
            )
            if result.ok:
                await execution_db.commit()
            else:
                await execution_db.rollback()
    except asyncio.CancelledError:
        await _mark_execution_unknown(
            session_factory,
            proposal_id,
            publisher,
            actor_user_id=actor_user_id,
            last_error="执行被取消",
            actor_ip=actor_ip,
        )
        raise
    except Exception as exc:
        # 这里的异常发生在执行器之外（事务/驱动等），无法判断命令是否已下发，
        # 保守按 UNKNOWN 处理；真实堆栈进服务端日志。
        logger.exception("HITL 执行异常 proposal_id=%s", proposal_id)
        return await _mark_execution_unknown(
            session_factory,
            proposal_id,
            publisher,
            actor_user_id=actor_user_id,
            last_error=type(exc).__name__,
            actor_ip=actor_ip,
        )

    if result is None or not result.ok:
        last_error = _describe_failure(result)
        retryable_reason: str | None = None
        if result is not None and not result.dispatched:
            # 执行器确认命令没发出去：设备没被碰过，回退到 APPROVED 可直接重试。
            retryable_reason = "dispatch_failed_before_send"
        elif prepared.action_type == "device_query":
            # 只读命令没有副作用：连上设备后失败（设备回「Unrecognized command」、读超时）
            # 也不必人工核实设备状态，回到 APPROVED 记下原因就能直接重试。
            retryable_reason = "read_only_failed"
        if retryable_reason is not None:
            return await _mark_execution_unexecuted(
                session_factory,
                proposal_id,
                publisher,
                actor_user_id=actor_user_id,
                last_error=last_error,
                actor_ip=actor_ip,
                reason=retryable_reason,
            )
        return await _mark_execution_unknown(
            session_factory,
            proposal_id,
            publisher,
            actor_user_id=actor_user_id,
            last_error=last_error,
            actor_ip=actor_ip,
        )

    try:
        async with session_factory() as finish_db:
            finished = await hitl_proposal_crud.mark_executed(finish_db, proposal_id)
            has_full_result = False
            if finished.action_type in ("device_query", "device_control"):
                output = result.detail.get("output")
                if isinstance(output, str):
                    updated_payload = dict(finished.action_payload)
                    updated_payload["last_result_excerpt"] = build_result_preview(output)
                    updated_payload.pop("last_error", None)
                    if updated_payload != finished.action_payload:
                        finished.action_payload = updated_payload
                        await finish_db.flush()
                    if finished.action_type == "device_query":
                        await hitl_execution_result_crud.create_for_proposal(
                            finish_db,
                            proposal_id=finished.id,
                            content=output,
                        )
                        has_full_result = True
            await log_audit(
                finish_db,
                actor_user_id,
                "hitl_executed",
                target=f"hitl_proposal:{finished.id}",
                detail=f"动作类型：{finished.action_type}",
                ip=actor_ip,
            )
            await finish_db.commit()
    except asyncio.CancelledError:
        await _mark_execution_unknown(
            session_factory, proposal_id, publisher, actor_user_id=actor_user_id, actor_ip=actor_ip
        )
        raise
    except Exception as exc:
        logger.warning(
            "HITL 执行收尾异常 proposal_id=%s exc_type=%s",
            proposal_id,
            type(exc).__name__,
        )
        return await _mark_execution_unknown(
            session_factory, proposal_id, publisher, actor_user_id=actor_user_id, actor_ip=actor_ip
        )

    await _publish_execution_summary(publisher, finished, has_full_result=has_full_result)
    return _summary(finished, has_full_result=has_full_result)


async def reconcile_executing_proposals(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """启动恢复：将所有 EXECUTING 提案转为 UNKNOWN。

    Args:
        session_factory: 数据库会话工厂。

    Returns:
        被恢复的提案数量。
    """
    async with session_factory() as db:
        changed = await hitl_proposal_crud.recover_executing(db)
        await db.commit()
    return changed
