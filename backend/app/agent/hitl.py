"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl.py
@DateTime: 2026-08-12 11:15
@Docs: HITL 提案编排：校验动作、复用状态机、调度执行器并发布安全事件。

实现流程：
1. propose_action 先合并顶层 asset_id，再用严格 Pydantic 模型校验动作载荷并检查 CMDB 资产。
2. 载荷校验失败只回传固定中文原因与字段名，绝不拼接 ValidationError / 原始 input_value。
3. 合法提案始终先以 PENDING 追加；assist/full 档位下按策略表自动批准并继续执行。
4. decide_proposal 只复用 CRUD 的审批状态机，不隐式恢复执行，避免人工 API 路径重复执行。
5. resume_proposal 仅执行 APPROVED 提案；EXECUTED 返回幂等摘要，其他状态明确拒绝。
6. 对 Agent 和事件发布器只暴露安全摘要，不返回原始 payload，避免设备凭据或未知字段泄露。
"""

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, cast
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.device_commands import (
    command_supports_vendor,
    command_type_of,
    get_device_command,
    list_command_names,
    list_commands_for_vendor,
    validate_interface_name,
)
from app.agent.executors import (
    DeviceQueryExecutor,
    ExecutionResult,
    NotifyExecutor,
)
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import device_command_policy_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.hitl_proposal import HitlProposal
from app.utils.audit import log_audit

type ActionType = Literal["notify", "device_control", "device_query"]


class NotifyPayload(BaseModel):
    """低风险通知动作的严格载荷。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    message: str = Field(min_length=1, max_length=2000)


class DeviceCommandPayload(BaseModel):
    """设备诊断/管控动作的严格载荷；两者共用同一形状。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    command_name: str = Field(min_length=1, max_length=100)
    interface_name: str | None = Field(default=None, min_length=1, max_length=64)


class HitlEventPublisher(Protocol):
    """HITL 状态事件发布协议。"""

    async def publish(
        self,
        *,
        session_id: int,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        """发布一个不含原始动作载荷的安全事件。"""
        ...


class NoopHitlEventPublisher:
    """默认空发布器，允许 T11 后续无侵入接入实时事件。"""

    async def publish(
        self,
        *,
        session_id: int,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        """忽略事件。"""
        return None


class HitlProposalRejectedError(ValueError):
    """提案在写入前因校验或 CMDB 查询失败而被拒绝。"""


class HitlResumeError(ValueError):
    """提案未处于可恢复状态时抛出。"""


@dataclass(frozen=True, slots=True)
class ProposalSafeSummary:
    """可安全返回给 Agent 或前端事件层的提案摘要。"""

    proposal_id: int
    action_type: ActionType
    status: str
    reason: str
    asset_id: int | None
    result_excerpt: str | None = None
    # 仅在执行失败后有值；内容是执行器的分类信息，不含原始异常/设备细节。
    last_error: str | None = None


_NOTIFY_EXECUTOR = NotifyExecutor()
_DEVICE_QUERY_EXECUTOR = DeviceQueryExecutor()
_EXECUTION_LOCKS: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def _execution_lock(proposal_id: int) -> asyncio.Lock:
    """返回进程内提案锁，补足 SQLite 不支持行锁的测试与单进程场景。"""
    lock = _EXECUTION_LOCKS.get(proposal_id)
    if lock is None:
        lock = asyncio.Lock()
        _EXECUTION_LOCKS[proposal_id] = lock
    return lock


def _summary(proposal: HitlProposal) -> ProposalSafeSummary:
    """从持久化对象提取白名单字段，绝不透传完整 payload。"""
    payload = proposal.action_payload
    raw_asset_id = payload.get("asset_id")
    asset_id = raw_asset_id if isinstance(raw_asset_id, int) and not isinstance(raw_asset_id, bool) else None
    raw_reason = payload.get("proposal_reason")
    reason = raw_reason if isinstance(raw_reason, str) else ""
    if proposal.action_type not in ("notify", "device_control", "device_query"):
        raise ValueError(f"不支持的 HITL 动作类型：{proposal.action_type}")
    action_type = cast(ActionType, proposal.action_type)
    raw_result_excerpt = payload.get("last_result_excerpt")
    result_excerpt = raw_result_excerpt if isinstance(raw_result_excerpt, str) else None
    raw_last_error = payload.get("last_error")
    last_error = raw_last_error if isinstance(raw_last_error, str) else None
    return ProposalSafeSummary(
        proposal_id=proposal.id,
        action_type=action_type,
        status=proposal.status,
        reason=reason,
        asset_id=asset_id,
        result_excerpt=result_excerpt,
        last_error=last_error,
    )


async def _publish(
    publisher: HitlEventPublisher | None,
    *,
    proposal: HitlProposal,
    event_type: str,
) -> None:
    """通过给定发布器发送安全摘要，未提供时使用空实现。"""
    event_publisher = publisher or NoopHitlEventPublisher()
    await event_publisher.publish(
        session_id=proposal.session_id,
        event_type=event_type,
        payload=asdict(_summary(proposal)),
    )


def _safe_validation_reason(exc: ValidationError) -> str:
    """将 Pydantic 校验失败映射为不含原始输入值的中文原因。

    只保留字段名，绝不拼接 ``str(ValidationError)``，避免 ``input_value``
    把密钥或其它敏感载荷回灌给 Agent 工具结果。
    """
    field_names: list[str] = []
    for error in exc.errors():
        loc = error.get("loc", ())
        parts = [str(part) for part in loc]
        if parts:
            field_names.append(".".join(parts))
    if not field_names:
        return "HITL 动作载荷校验失败"
    # dict.fromkeys 保序去重，避免同一字段重复出现在拒绝原因里。
    unique_fields = "、".join(dict.fromkeys(field_names))
    return f"HITL 动作载荷校验失败，涉及字段：{unique_fields}"


def _validated_payload(
    *,
    action_type: ActionType,
    asset_id: int,
    payload: Mapping[str, object],
    reason: str,
) -> dict[str, object]:
    """执行顶层资产合并规则和严格动作载荷校验。"""
    if not isinstance(asset_id, int) or isinstance(asset_id, bool):
        raise HitlProposalRejectedError("asset_id 必须是整数")

    candidate = dict(payload)
    payload_asset_id = candidate.pop("asset_id", asset_id)
    if payload_asset_id != asset_id:
        raise HitlProposalRejectedError("payload.asset_id 与顶层 asset_id 不一致")

    try:
        if action_type == "notify":
            validated = NotifyPayload.model_validate(candidate).model_dump()
        elif action_type in ("device_control", "device_query"):
            validated = DeviceCommandPayload.model_validate(candidate).model_dump()
        else:
            raise HitlProposalRejectedError(f"不支持的 HITL 动作类型：{action_type}")
    except ValidationError as exc:
        raise HitlProposalRejectedError(_safe_validation_reason(exc)) from exc

    return {**validated, "asset_id": asset_id, "proposal_reason": reason}


def should_auto_approve(
    *,
    approval_mode: str,
    action_type: ActionType,
    policy_decision: str | None,
    credential_type: str,
) -> bool:
    """按会话审批档位判定提案是否可自动批准并执行。

    Args:
        approval_mode: 会话审批档位（ask / assist / full）。
        action_type: HITL 动作类型。
        policy_decision: 设备命令策略判定；notify 传入 None。
        credential_type: 资产凭据类型。

    Returns:
        True 表示可自动批准并继续执行。
    """
    if action_type == "notify":
        return approval_mode in ("assist", "full")
    if action_type in ("device_query", "device_control"):
        if credential_type == "dynamic":
            return False
        if policy_decision == "whitelist":
            return approval_mode in ("assist", "full")
        if policy_decision is None:
            return approval_mode == "full"
        return False
    return False


async def propose_action(
    db: AsyncSession,
    *,
    session_id: int,
    proposed_by_agent_id: str | None,
    action_type: ActionType,
    asset_id: int,
    payload: Mapping[str, object],
    reason: str,
    actor_user_id: int,
    publisher: HitlEventPublisher | None = None,
) -> ProposalSafeSummary:
    """创建经过严格校验的 HITL 提案，并按策略处理低风险通知。

    Args:
        db: 调用方事务内的数据库会话。
        session_id: 提案所属 Agent 会话 ID。
        proposed_by_agent_id: 发起提案的子 Agent ID，可为空。
        action_type: 支持 notify 或 device_control。
        asset_id: 顶层 CMDB 资产 ID。
        payload: 不含资产 ID 的动作参数；允许冗余传入相同资产 ID。
        reason: Agent 提案原因。
        actor_user_id: 发起上下文中的真实用户 ID，自动审批时作为审批人。
        publisher: 可选的安全事件发布器。

    Returns:
        当前最终状态的安全提案摘要。

    Raises:
        HitlProposalRejectedError: 用户 ID、动作载荷或 CMDB 资产不合法时。
    """
    if not isinstance(actor_user_id, int) or isinstance(actor_user_id, bool):
        raise HitlProposalRejectedError("actor_user_id 必须是真实整数用户 ID")

    stored_payload = _validated_payload(
        action_type=action_type,
        asset_id=asset_id,
        payload=payload,
        reason=reason,
    )
    asset = await cmdb_asset_crud.get(db, asset_id)
    if asset is None:
        raise HitlProposalRejectedError(f"CMDB 资产不存在：{asset_id}")

    if action_type in ("device_query", "device_control"):
        command_name = stored_payload["command_name"]
        assert isinstance(command_name, str)

        command_type = command_type_of(command_name)
        if command_type is None:
            raise HitlProposalRejectedError(f"未知命令名：{command_name}；可用命令：{'、'.join(list_command_names())}")
        if action_type == "device_query" and command_type != "read_only":
            raise HitlProposalRejectedError(
                "会改变设备状态的命令请使用 propose_device_control 工具，不能用 query_device_command"
            )
        if action_type == "device_control" and command_type != "state_changing":
            raise HitlProposalRejectedError("只读命令请使用 query_device_command 工具，不需要走 propose_device_control")

        if asset.credential_type == "none":
            raise HitlProposalRejectedError("该资产未配置登录凭据，无法执行设备命令")
        if not asset.vendor:
            raise HitlProposalRejectedError("资产未配置厂商信息，无法确定命令语法")
        if not command_supports_vendor(command_name, asset.vendor):
            supported = list_commands_for_vendor(asset.vendor)
            supported_hint = (
                f"该厂商支持的命令：{'、'.join(item.name for item in supported)}"
                if supported
                else "该厂商当前没有任何可用命令"
            )
            raise HitlProposalRejectedError(
                f"该设备厂商不支持这个命令（厂商 {asset.vendor}，命令 {command_name}）；{supported_hint}"
            )

        definition = get_device_command(command_name)
        interface_name = stored_payload.get("interface_name")
        if definition.requires_argument == "interface_name":
            if not isinstance(interface_name, str) or not validate_interface_name(interface_name):
                raise HitlProposalRejectedError("port_enable/port_disable 需要合法的接口名参数")
        elif interface_name is not None:
            raise HitlProposalRejectedError(f"命令 {command_name} 不接受 interface_name 参数")

        policy_decision = await device_command_policy_crud.resolve_policy(
            db, asset_id=asset.id, asset_type=asset.asset_type, command_name=command_name
        )
        if policy_decision == "blacklist":
            raise HitlProposalRejectedError("该命令已被列入黑名单，禁止执行")
    else:
        policy_decision = None

    session = await agent_session_crud.get(db, session_id)
    if session is None:
        raise HitlProposalRejectedError("会话不存在")
    approval_mode = session.approval_mode

    proposal = await hitl_proposal_crud.create(
        db,
        session_id=session_id,
        proposed_by_agent_id=proposed_by_agent_id,
        action_type=action_type,
        action_payload=stored_payload,
    )
    await _publish(publisher, proposal=proposal, event_type="hitl_pending")

    if should_auto_approve(
        approval_mode=approval_mode,
        action_type=action_type,
        policy_decision=policy_decision,
        credential_type=asset.credential_type,
    ):
        await decide_proposal(
            db,
            proposal_id=proposal.id,
            approve=True,
            reviewed_by_user_id=actor_user_id,
            publisher=publisher,
        )
        return await resume_proposal(
            db,
            proposal_id=proposal.id,
            actor_user_id=actor_user_id,
            publisher=publisher,
        )

    return _summary(proposal)


async def decide_proposal(
    db: AsyncSession,
    *,
    proposal_id: int,
    approve: bool,
    reviewed_by_user_id: int,
    publisher: HitlEventPublisher | None = None,
) -> ProposalSafeSummary:
    """审批提案但不自动恢复执行。

    Args:
        db: 调用方事务内的数据库会话。
        proposal_id: 待审批提案 ID。
        approve: True 表示批准，False 表示拒绝。
        reviewed_by_user_id: 真实审批用户 ID。
        publisher: 可选的安全事件发布器。

    Returns:
        审批后的安全提案摘要。
    """
    proposal = await hitl_proposal_crud.decide(
        db,
        proposal_id,
        approve=approve,
        reviewed_by_user_id=reviewed_by_user_id,
    )
    action = "hitl_approved" if approve else "hitl_rejected"
    await log_audit(
        db,
        reviewed_by_user_id,
        action,
        target=f"hitl_proposal:{proposal.id}",
        detail=f"动作类型：{proposal.action_type}",
    )
    if not approve:
        await _publish(publisher, proposal=proposal, event_type="hitl_resolved")
    return _summary(proposal)


async def resume_proposal(
    db: AsyncSession,
    *,
    proposal_id: int,
    actor_user_id: int | None = None,
    publisher: HitlEventPublisher | None = None,
    dynamic_password: str | None = None,
) -> ProposalSafeSummary:
    """幂等恢复一个已批准提案并调度对应执行器。

    Args:
        db: 调用方事务内的数据库会话。
        proposal_id: 待恢复提案 ID。
        actor_user_id: 触发恢复的用户 ID，可为空。
        publisher: 可选的安全事件发布器。
        dynamic_password: 动态凭据资产执行时的一次性明文密码，不落库。

    Returns:
        执行成功后的 EXECUTED 摘要，或执行失败后仍为 APPROVED 的摘要。

    Raises:
        HitlResumeError: 提案不存在或状态不是 APPROVED/EXECUTED 时。
    """
    async with _execution_lock(proposal_id):
        stmt = (
            select(HitlProposal)
            .where(HitlProposal.id == proposal_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await db.execute(stmt)
        proposal = result.scalar_one_or_none()
        if proposal is None:
            raise HitlResumeError(f"HITL 提案不存在：{proposal_id}")
        if proposal.status == "EXECUTED":
            return _summary(proposal)
        if proposal.status != "APPROVED":
            raise HitlResumeError(f"状态 {proposal.status} 的 HITL 提案不可恢复执行")

        if proposal.action_type == "notify":
            execution_result = await _NOTIFY_EXECUTOR.execute(
                db,
                proposal_id=proposal.id,
                payload=proposal.action_payload,
                actor_user_id=actor_user_id,
            )
        elif proposal.action_type in ("device_query", "device_control"):
            raw_asset_id = proposal.action_payload.get("asset_id")
            asset_for_query = await cmdb_asset_crud.get(db, raw_asset_id) if isinstance(raw_asset_id, int) else None
            if asset_for_query is None:
                execution_result = ExecutionResult(ok=False, message="资产不存在")
            else:
                raw_command_name = proposal.action_payload.get("command_name")
                raw_interface_name = proposal.action_payload.get("interface_name")
                execution_result = await _DEVICE_QUERY_EXECUTOR.execute(
                    db,
                    asset=asset_for_query,
                    command_name=str(raw_command_name),
                    dynamic_password=dynamic_password,
                    interface_name=raw_interface_name if isinstance(raw_interface_name, str) else None,
                )
        else:
            raise HitlResumeError(f"不支持的 HITL 动作类型：{proposal.action_type}")

        if execution_result.ok:
            proposal = await hitl_proposal_crud.mark_executed(db, proposal.id)
            updated_payload = dict(proposal.action_payload)
            # 重试成功后清除上一次的失败标记
            updated_payload.pop("last_error", None)
            if proposal.action_type in ("device_query", "device_control"):
                output = execution_result.detail.get("output")
                if isinstance(output, str):
                    updated_payload["last_result_excerpt"] = output
            if updated_payload != proposal.action_payload:
                proposal.action_payload = updated_payload
                await db.flush()
            await log_audit(
                db,
                actor_user_id,
                "hitl_executed",
                target=f"hitl_proposal:{proposal.id}",
                detail=f"动作类型：{proposal.action_type}",
            )
            await _publish(publisher, proposal=proposal, event_type="hitl_resolved")
        else:
            # 把执行器的分类失败信息写回 payload，让后续回查/工具文案能区分
            # "已批准等待执行" 与 "执行过但失败"；成功后覆盖为无错误。
            proposal.action_payload = {
                **proposal.action_payload,
                "last_error": execution_result.message,
            }
            await db.flush()
            await log_audit(
                db,
                actor_user_id,
                "hitl_execution_failed",
                target=f"hitl_proposal:{proposal.id}",
                detail=execution_result.message,
            )
            await _publish(publisher, proposal=proposal, event_type="hitl_execution_failed")

        return _summary(proposal)
