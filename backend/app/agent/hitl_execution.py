"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl_execution.py
@DateTime: 2026-08-14
@Docs: HITL 独立执行服务：策略复检、持久化认领、外部执行与 UNKNOWN 恢复。

实现流程：
1. execute_approved_proposal 先经 _preflight_and_claim 在同一短事务内复检策略并认领 EXECUTING。
2. 认领提交后 _execute_prepared 调用注入或默认执行器；执行器内可观测已提交的 EXECUTING 状态。
3. 执行成功后再开事务 mark_executed；执行失败或异常统一 mark_unknown(dispatch_outcome_unknown)。
4. reconcile_executing_proposals 供启动恢复，将遗留 EXECUTING 批量转为 UNKNOWN。
5. 预检失败（命令不存在、动态凭据缺失）不认领，提案保持 APPROVED。
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import make_transient

from app.agent.device_commands import (
    command_supports_vendor,
    command_type_of,
    get_device_command,
    validate_interface_name,
)
from app.agent.executors import DeviceQueryExecutor, ExecutionResult, NotifyExecutor
from app.agent.hitl import (
    ActionType,
    HitlEventPublisher,
    HitlResumeError,
    ProposalSafeSummary,
    _publish,
    _summary,
)
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import device_command_policy_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.cmdb_asset import CmdbAsset
from app.models.hitl_proposal import HitlProposal
from app.utils.audit import log_audit

logger = logging.getLogger(__name__)


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
        interface_name: str | None = None,
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


def _detach_asset(asset: CmdbAsset) -> CmdbAsset:
    """从会话中分离资产副本，供后续独立事务使用。"""
    make_transient(asset)
    return asset


async def _preflight_and_claim(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    proposal_id: int,
    dynamic_password: str | None,
    publisher: HitlEventPublisher | None,
) -> PreparedExecution | ProposalSafeSummary:
    """在同一短事务内完成复检、策略拒绝或认领 EXECUTING 并提交。

    Args:
        session_factory: 独立短会话工厂。
        proposal_id: 待执行提案 ID。
        dynamic_password: 动态凭据明文，不落库。
        publisher: 可选安全事件发布器。

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
            return _summary(proposal)
        if proposal.status == "EXECUTING":
            for _ in range(200):
                await asyncio.sleep(0.05)
                db.expire_all()
                refreshed = await hitl_proposal_crud.get(db, proposal_id)
                if refreshed is None:
                    raise HitlResumeError(f"HITL 提案不存在：{proposal_id}")
                if refreshed.status == "EXECUTED":
                    return _summary(refreshed)
                if refreshed.status == "APPROVED":
                    proposal = refreshed
                    break
                if refreshed.status == "UNKNOWN":
                    raise HitlResumeError(f"状态 UNKNOWN 的 HITL 提案不可恢复执行")
            else:
                raise HitlResumeError("等待并发执行完成超时")
            if proposal.status == "EXECUTING":
                raise HitlResumeError(f"状态 {proposal.status} 的 HITL 提案不可恢复执行")
        if proposal.status != "APPROVED":
            raise HitlResumeError(f"状态 {proposal.status} 的 HITL 提案不可恢复执行")

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

            definition = get_device_command(command_name)
            interface_name = proposal.action_payload.get("interface_name")
            if definition.requires_argument == "interface_name":
                if not isinstance(interface_name, str) or not validate_interface_name(interface_name):
                    return _summary(proposal)
            elif interface_name is not None:
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

        claimed = await hitl_proposal_crud.claim_execution(db, proposal_id)
        await db.commit()

        copied_payload = dict(claimed.action_payload)
        if action_type not in ("notify", "device_control", "device_query"):
            raise HitlResumeError(f"不支持的 HITL 动作类型：{action_type}")

        return PreparedExecution(
            proposal_id=claimed.id,
            session_id=claimed.session_id,
            action_type=action_type,
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
        raw_command_name = prepared.payload.get("command_name")
        raw_interface_name = prepared.payload.get("interface_name")
        return await device_executor.execute(
            db,
            asset=prepared.asset,
            command_name=str(raw_command_name),
            dynamic_password=dynamic_password,
            interface_name=raw_interface_name if isinstance(raw_interface_name, str) else None,
        )

    raise HitlResumeError(f"不支持的 HITL 动作类型：{prepared.action_type}")


async def _mark_execution_unknown(
    session_factory: async_sessionmaker[AsyncSession],
    proposal_id: int,
    publisher: HitlEventPublisher | None,
) -> ProposalSafeSummary:
    """将 EXECUTING 提案标记为 UNKNOWN 并发布安全事件。

    Args:
        session_factory: 独立短会话工厂。
        proposal_id: 提案 ID。
        publisher: 可选安全事件发布器。

    Returns:
        UNKNOWN 状态的安全摘要。
    """
    async with session_factory() as db:
        unknown = await hitl_proposal_crud.mark_unknown(
            db,
            proposal_id,
            reason="dispatch_outcome_unknown",
        )
        await db.commit()

    await _publish(publisher, proposal=unknown, event_type="hitl_execution_failed")
    return _summary(unknown)


async def _publish_execution_summary(
    publisher: HitlEventPublisher | None,
    proposal: HitlProposal,
) -> None:
    """发布执行成功后的安全摘要事件。"""
    await _publish(publisher, proposal=proposal, event_type="hitl_resolved")


async def execute_approved_proposal(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    proposal_id: int,
    actor_user_id: int | None,
    publisher: HitlEventPublisher | None = None,
    dynamic_password: str | None = None,
    notify_executor: NotifyExecutorProtocol | None = None,
    device_executor: DeviceExecutorProtocol | None = None,
) -> ProposalSafeSummary:
    """执行已批准提案：复检策略、认领 EXECUTING、调用外部执行器并完成状态迁移。

    Args:
        session_factory: 独立短会话工厂。
        proposal_id: 待执行提案 ID。
        actor_user_id: 触发执行的用户 ID。
        publisher: 可选安全事件发布器。
        dynamic_password: 动态凭据明文，不落库。
        notify_executor: 可选通知执行器。
        device_executor: 可选设备执行器。

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
        await _mark_execution_unknown(session_factory, proposal_id, publisher)
        raise
    except Exception as exc:
        logger.warning(
            "HITL 执行异常 proposal_id=%s exc_type=%s",
            proposal_id,
            type(exc).__name__,
        )
        return await _mark_execution_unknown(session_factory, proposal_id, publisher)

    if result is None or not result.ok:
        return await _mark_execution_unknown(session_factory, proposal_id, publisher)

    async with session_factory() as finish_db:
        finished = await hitl_proposal_crud.mark_executed(finish_db, proposal_id)
        if finished.action_type in ("device_query", "device_control"):
            output = result.detail.get("output")
            if isinstance(output, str):
                updated_payload = dict(finished.action_payload)
                updated_payload["last_result_excerpt"] = output
                updated_payload.pop("last_error", None)
                if updated_payload != finished.action_payload:
                    finished.action_payload = updated_payload
                    await finish_db.flush()
        await log_audit(
            finish_db,
            actor_user_id,
            "hitl_executed",
            target=f"hitl_proposal:{finished.id}",
            detail=f"动作类型：{finished.action_type}",
        )
        await finish_db.commit()

    await _publish_execution_summary(publisher, finished)
    return _summary(finished)


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
