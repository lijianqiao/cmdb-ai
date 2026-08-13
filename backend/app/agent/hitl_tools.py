"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl_tools.py
@DateTime: 2026-08-12 11:26
@Docs: 将根 Agent 的整改建议转换为既有 HITL 提案，并返回安全工具结果。

实现流程：
1. 根 Agent 提供资产、动作、载荷和原因，可信会话身份由根调度器绑定。
2. 工具调用 hitl.propose_action 复用唯一的校验、审批和执行状态机。
3. 待人工处理时返回 pending_approval；通知已自动执行时返回 ok。
4. 预期校验错误保留可操作原因，意外错误仅暴露异常类型，且绝不回传原始载荷。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.device_commands import (
    command_supports_vendor,
)
from app.agent.device_commands import (
    list_device_commands as list_catalog_commands,
)
from app.agent.hitl import (
    ActionType,
    HitlEventPublisher,
    HitlProposalRejectedError,
    ProposalSafeSummary,
    propose_action,
)
from app.agent.loop import ToolResult
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import device_command_policy_crud


def _execution_failure_text(kind: str, summary: ProposalSafeSummary) -> str:
    """区分"已批准但执行失败"与其它未完成状态，避免模型误报"正在执行中"。"""
    if summary.status == "APPROVED" and summary.last_error:
        return (
            f"{kind} {summary.proposal_id} 已批准但上次执行失败：{summary.last_error}，需人工处理。"
            "系统不会自动重试，请如实告知用户执行失败，"
            "由管理员排查设备连通性/凭据后在审批卡片上重试执行。"
        )
    return f"{kind} {summary.proposal_id} 当前状态为 {summary.status}，未完成执行。"


async def propose_remediation(
    db: AsyncSession,
    *,
    session_id: int,
    actor_user_id: int,
    proposed_by_agent_id: str | None,
    asset_id: int,
    action_type: ActionType,
    payload: dict[str, object],
    reason: str,
    publisher: HitlEventPublisher | None = None,
) -> ToolResult:
    """提交根 Agent 整改提案并生成不含敏感载荷的工具结果。

    Args:
        db: 当前事务使用的异步数据库会话。
        session_id: 根 Agent 所属会话 ID。
        actor_user_id: 当前认证用户 ID。
        proposed_by_agent_id: 发起提案的 Agent ID，可为空。
        asset_id: 整改目标的 CMDB 资产 ID。
        action_type: 通知或设备控制动作类型。
        payload: 动作参数，不会写入工具返回内容。
        reason: 发起整改的原因。
        publisher: 可选的 HITL 安全事件发布器。

    Returns:
        指示待审批、已执行、被拒绝或失败的安全工具结果。
    """
    try:
        summary = await propose_action(
            db,
            session_id=session_id,
            actor_user_id=actor_user_id,
            proposed_by_agent_id=proposed_by_agent_id,
            asset_id=asset_id,
            action_type=action_type,
            payload=payload,
            reason=reason,
            publisher=publisher,
        )
    except HitlProposalRejectedError as exc:
        return ToolResult(control="rejected", content=f"整改提案被拒绝：{exc}")
    except Exception as exc:
        return ToolResult(
            control="failed",
            content=f"整改提案创建失败：{type(exc).__name__}",
        )

    if summary.status == "EXECUTED" and summary.action_type == "notify":
        return ToolResult(
            control="ok",
            content=f"整改提案 {summary.proposal_id} 已自动批准并执行通知。",
        )
    if summary.status == "PENDING":
        return ToolResult(
            control="pending_approval",
            content=f"整改提案 {summary.proposal_id} 已创建，正在等待人工审批。",
        )
    return ToolResult(
        control="failed",
        content=_execution_failure_text("整改提案", summary),
    )


async def query_device_command(
    db: AsyncSession,
    *,
    session_id: int,
    actor_user_id: int,
    proposed_by_agent_id: str | None,
    asset_id: int,
    command_name: str,
    reason: str,
    publisher: HitlEventPublisher | None = None,
) -> ToolResult:
    """对已配置凭据的资产发起只读诊断命令查询。

    白名单命中且资产非动态凭据时会在这次调用里直接执行完成，返回 ok 并
    附带命令输出；其它情况停在 pending_approval，需要人工审批（动态凭据
    资产还需要在批准时当场输入密码）。

    Args:
        db: 当前事务使用的异步数据库会话。
        session_id: 根 Agent 所属会话 ID。
        actor_user_id: 当前认证用户 ID。
        proposed_by_agent_id: 发起提案的 Agent ID，可为空。
        asset_id: 目标 CMDB 资产 ID。
        command_name: 白名单内的只读诊断命令名。
        reason: 发起查询的原因。
        publisher: 可选的 HITL 安全事件发布器。

    Returns:
        指示待审批、已执行、被拒绝或失败的安全工具结果。
    """
    try:
        summary = await propose_action(
            db,
            session_id=session_id,
            actor_user_id=actor_user_id,
            proposed_by_agent_id=proposed_by_agent_id,
            asset_id=asset_id,
            action_type="device_query",
            payload={"command_name": command_name},
            reason=reason,
            publisher=publisher,
        )
    except HitlProposalRejectedError as exc:
        return ToolResult(control="rejected", content=f"设备命令查询被拒绝：{exc}")
    except Exception as exc:
        return ToolResult(
            control="failed",
            content=f"设备命令查询创建失败：{type(exc).__name__}",
        )

    if summary.status == "EXECUTED":
        output = summary.result_excerpt or "（无输出）"
        return ToolResult(
            control="ok",
            content=f"设备命令 {summary.proposal_id} 已自动批准并执行：\n{output}",
        )
    if summary.status == "PENDING":
        return ToolResult(
            control="pending_approval",
            content=f"设备命令查询 {summary.proposal_id} 已创建，正在等待人工审批。",
        )
    return ToolResult(
        control="failed",
        content=_execution_failure_text("设备命令查询", summary),
    )


async def propose_device_control(
    db: AsyncSession,
    *,
    session_id: int,
    actor_user_id: int,
    proposed_by_agent_id: str | None,
    asset_id: int,
    command_name: str,
    interface_name: str | None,
    reason: str,
    publisher: HitlEventPublisher | None = None,
) -> ToolResult:
    """对已配置凭据的资产发起会改变设备状态的命令（reboot/shutdown/port_enable/port_disable）。

    命中白名单且资产非动态凭据时当场执行；否则停在 pending_approval，
    需要人工审批（动态凭据资产还需要在批准时当场输入密码）。

    Args:
        db: 当前事务使用的异步数据库会话。
        session_id: 根 Agent 所属会话 ID。
        actor_user_id: 当前认证用户 ID。
        proposed_by_agent_id: 发起提案的 Agent ID，可为空。
        asset_id: 目标 CMDB 资产 ID。
        command_name: 白名单内的变更类命令名。
        interface_name: port_enable/port_disable 所需的接口名，其它命令应为空。
        reason: 发起管控的原因。
        publisher: 可选的 HITL 安全事件发布器。

    Returns:
        指示待审批、已执行、被拒绝或失败的安全工具结果。
    """
    payload: dict[str, object] = {"command_name": command_name}
    if interface_name is not None:
        payload["interface_name"] = interface_name

    try:
        summary = await propose_action(
            db,
            session_id=session_id,
            actor_user_id=actor_user_id,
            proposed_by_agent_id=proposed_by_agent_id,
            asset_id=asset_id,
            action_type="device_control",
            payload=payload,
            reason=reason,
            publisher=publisher,
        )
    except HitlProposalRejectedError as exc:
        return ToolResult(control="rejected", content=f"设备管控请求被拒绝：{exc}")
    except Exception as exc:
        return ToolResult(
            control="failed",
            content=f"设备管控请求创建失败：{type(exc).__name__}",
        )

    if summary.status == "EXECUTED":
        output = summary.result_excerpt or "（无输出）"
        return ToolResult(
            control="ok",
            content=f"设备管控命令 {summary.proposal_id} 已自动批准并执行：\n{output}",
        )
    if summary.status == "PENDING":
        return ToolResult(
            control="pending_approval",
            content=f"设备管控请求 {summary.proposal_id} 已创建，正在等待人工审批。",
        )
    return ToolResult(
        control="failed",
        content=_execution_failure_text("设备管控请求", summary),
    )


async def get_device_query_result(
    db: AsyncSession,
    *,
    session_id: int,
    proposal_id: int,
) -> ToolResult:
    """按会话回查一个已提交的设备命令查询提案的当前结果。

    只读、无审批要求；跟别的会话的提案严格隔离，不匹配当成"不存在"处理，
    不泄露其它会话的提案是否存在。

    Args:
        db: 当前事务使用的异步数据库会话。
        session_id: 根 Agent 所属会话 ID。
        proposal_id: 待回查的提案 ID。

    Returns:
        提案当前状态或执行结果的安全工具结果。
    """
    from app.crud.hitl_proposal import hitl_proposal_crud

    proposal = await hitl_proposal_crud.get(db, proposal_id)
    if proposal is None or proposal.session_id != session_id:
        return ToolResult(control="rejected", content="提案不存在")

    if proposal.status == "EXECUTED":
        excerpt = proposal.action_payload.get("last_result_excerpt") or "（无输出）"
        return ToolResult(control="ok", content=f"提案 {proposal_id} 已执行：\n{excerpt}")
    if proposal.status == "REJECTED":
        return ToolResult(control="ok", content=f"提案 {proposal_id} 已被拒绝")
    if proposal.status == "PENDING":
        return ToolResult(
            control="ok",
            content=f"提案 {proposal_id} 正在等待人工审批，尚未执行。",
        )
    if proposal.status == "APPROVED":
        last_error = proposal.action_payload.get("last_error")
        if isinstance(last_error, str) and last_error:
            return ToolResult(
                control="ok",
                content=(
                    f"提案 {proposal_id} 已批准但上次执行失败：{last_error}，需人工处理。"
                    "系统不会自动重试，需要管理员排查设备连通性/凭据后在审批卡片上重试执行。"
                ),
            )
        return ToolResult(
            control="ok",
            content=(
                f"提案 {proposal_id} 已批准但未执行成功，需人工在审批卡片上重试执行。"
                "这不是正在执行中。"
            ),
        )
    return ToolResult(
        control="ok",
        content=f"提案 {proposal_id} 当前状态：{proposal.status}",
    )


async def list_device_commands_for_asset(
    db: AsyncSession,
    *,
    asset_id: int,
) -> ToolResult:
    """列出一台资产可用的诊断命令、审批策略与凭据前提。

    只读、无审批要求；让模型先查询"这台设备能做什么、要不要人工审批"，
    而不是靠猜命令名反复失败。

    Args:
        db: 当前事务使用的异步数据库会话。
        asset_id: 目标 CMDB 资产 ID。

    Returns:
        含命令名、说明、审批策略与凭据状态的安全工具结果。
    """
    asset = await cmdb_asset_crud.get(db, asset_id)
    if asset is None:
        return ToolResult(control="rejected", content=f"CMDB 资产不存在：{asset_id}")

    if not asset.vendor:
        return ToolResult(
            control="rejected",
            content=(
                f"资产 {asset_id}（{asset.hostname}）未配置厂商信息，无法确定命令语法；请先在 CMDB 中补全 vendor 字段。"
            ),
        )

    lines = [f"资产 {asset_id}（{asset.hostname}，厂商 {asset.vendor}）可用诊断命令："]
    supported_any = False
    for definition in list_catalog_commands():
        if not command_supports_vendor(definition.name, asset.vendor):
            continue
        supported_any = True
        decision = await device_command_policy_crud.resolve_policy(
            db,
            asset_id=asset.id,
            asset_type=asset.asset_type,
            command_name=definition.name,
        )
        if decision == "blacklist":
            policy_label = "黑名单（禁止执行）"
        elif decision == "whitelist":
            policy_label = "白名单（自动执行）"
        else:
            policy_label = "需人工审批"
        lines.append(f"- {definition.name}：{definition.description}；策略：{policy_label}")

    if not supported_any:
        return ToolResult(
            control="ok",
            content=f"厂商 {asset.vendor} 当前没有任何可用命令。",
        )

    if asset.credential_type == "none":
        lines.append("注意：该资产未配置登录凭据，执行任何命令前需先在 CMDB 中配置凭据。")
    elif asset.credential_type == "dynamic":
        lines.append("注意：该资产使用动态凭据，所有命令都需要人工审批并当场输入密码。")

    return ToolResult(control="ok", content="\n".join(lines))
