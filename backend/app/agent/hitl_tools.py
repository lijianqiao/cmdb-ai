"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl_tools.py
@DateTime: 2026-08-12 11:26
@Docs: 根 Agent 执行类薄工具：门控前路径已统一由 HitlGateHook 执行。

实现流程：
1. 模型调用 notify / device_control / query_device_command；提案与执行均由门控 before 完成。
2. 薄工具保留供直接单元测试；正常聊天路径不会到达此处。
3. 回查与列表工具仍为只读辅助，不涉及审批或执行。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.device_commands import command_supports_vendor
from app.agent.device_commands import list_device_commands as list_catalog_commands
from app.agent.executors import DeviceQueryExecutor, NotifyExecutor
from app.agent.hitl import HitlEventPublisher
from app.agent.hitl_gate import HitlGateHook
from app.agent.loop import ToolResult
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import device_command_policy_crud

_NOTIFY_EXECUTOR = NotifyExecutor()
_DEVICE_QUERY_EXECUTOR = DeviceQueryExecutor()


def _execution_failure_text(kind: str, proposal_id: int, last_error: str | None, status: str) -> str:
    """区分已批准但执行失败与其它未完成状态。"""
    if status == "APPROVED" and last_error:
        return (
            f"{kind} {proposal_id} 已批准但上次执行失败：{last_error}，需人工处理。"
            "系统不会自动重试，请如实告知用户执行失败，"
            "由管理员排查设备连通性/凭据后在审批卡片上重试执行。"
        )
    return f"{kind} {proposal_id} 当前状态为 {status}，未完成执行。"


async def notify(
    db: AsyncSession,
    *,
    session_id: int,
    actor_user_id: int,
    proposed_by_agent_id: str | None,
    asset_id: int,
    payload: dict[str, object],
    reason: str,
    publisher: HitlEventPublisher | None = None,
    gate_hook: HitlGateHook | None = None,
) -> ToolResult:
    """执行已门控批准的通知动作。

    Args:
        db: 当前事务使用的异步数据库会话。
        session_id: 根 Agent 所属会话 ID。
        actor_user_id: 当前认证用户 ID。
        proposed_by_agent_id: 发起提案的 Agent ID，可为空。
        asset_id: 通知目标的 CMDB 资产 ID。
        payload: 须含 message 字段。
        reason: 发起通知的原因。
        publisher: 可选的 HITL 安全事件发布器。
        gate_hook: 门控钩子，提供当前提案 ID。

    Returns:
        执行成功或失败的安全工具结果。
    """
    proposal_id = gate_hook.current_proposal_id if gate_hook is not None else None
    if proposal_id is None:
        return ToolResult(control="failed", content="通知执行缺少门控提案上下文")

    execution_payload = {
        **payload,
        "asset_id": asset_id,
        "proposal_reason": reason,
    }
    execution_result = await _NOTIFY_EXECUTOR.execute(
        db,
        proposal_id=proposal_id,
        payload=execution_payload,
        actor_user_id=actor_user_id,
    )
    if execution_result.ok:
        return ToolResult(
            control="ok",
            content=f"通知提案 {proposal_id} 已自动批准并执行。",
        )
    return ToolResult(
        control="failed",
        content=_execution_failure_text("通知提案", proposal_id, execution_result.message, "APPROVED"),
    )


async def device_control(
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
    gate_hook: HitlGateHook | None = None,
) -> ToolResult:
    """执行已门控批准的设备变更命令。

    Args:
        db: 当前事务使用的异步数据库会话。
        session_id: 根 Agent 所属会话 ID。
        actor_user_id: 当前认证用户 ID。
        proposed_by_agent_id: 发起提案的 Agent ID，可为空。
        asset_id: 目标 CMDB 资产 ID。
        command_name: 白名单内的变更类命令名。
        interface_name: port_enable/port_disable 所需的接口名。
        reason: 发起管控的原因。
        publisher: 可选的 HITL 安全事件发布器。
        gate_hook: 门控钩子，提供当前提案 ID。

    Returns:
        执行成功或失败的安全工具结果。
    """
    proposal_id = gate_hook.current_proposal_id if gate_hook is not None else None
    if proposal_id is None:
        return ToolResult(control="failed", content="设备管控执行缺少门控提案上下文")

    asset = await cmdb_asset_crud.get(db, asset_id)
    if asset is None:
        return ToolResult(control="failed", content=f"CMDB 资产不存在：{asset_id}")

    execution_result = await _DEVICE_QUERY_EXECUTOR.execute(
        db,
        asset=asset,
        command_name=command_name,
        dynamic_password=None,
        interface_name=interface_name,
    )
    if execution_result.ok:
        output = execution_result.detail.get("output")
        excerpt = output if isinstance(output, str) else "（无输出）"
        return ToolResult(
            control="ok",
            content=f"设备管控命令 {proposal_id} 已自动批准并执行：\n{excerpt}",
        )
    return ToolResult(
        control="failed",
        content=_execution_failure_text("设备管控请求", proposal_id, execution_result.message, "APPROVED"),
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
    gate_hook: HitlGateHook | None = None,
) -> ToolResult:
    """执行已门控批准的只读设备诊断命令。

    Args:
        db: 当前事务使用的异步数据库会话。
        session_id: 根 Agent 所属会话 ID。
        actor_user_id: 当前认证用户 ID。
        proposed_by_agent_id: 发起提案的 Agent ID，可为空。
        asset_id: 目标 CMDB 资产 ID。
        command_name: 白名单内的只读诊断命令名。
        reason: 发起查询的原因。
        publisher: 可选的 HITL 安全事件发布器。
        gate_hook: 门控钩子，提供当前提案 ID。

    Returns:
        执行成功或失败的安全工具结果。
    """
    proposal_id = gate_hook.current_proposal_id if gate_hook is not None else None
    if proposal_id is None:
        return ToolResult(control="failed", content="设备命令查询缺少门控提案上下文")

    asset = await cmdb_asset_crud.get(db, asset_id)
    if asset is None:
        return ToolResult(control="failed", content=f"CMDB 资产不存在：{asset_id}")

    execution_result = await _DEVICE_QUERY_EXECUTOR.execute(
        db,
        asset=asset,
        command_name=command_name,
        dynamic_password=None,
    )
    if execution_result.ok:
        output = execution_result.detail.get("output")
        excerpt = output if isinstance(output, str) else "（无输出）"
        return ToolResult(
            control="ok",
            content=f"设备命令 {proposal_id} 已自动批准并执行：\n{excerpt}",
        )
    return ToolResult(
        control="failed",
        content=_execution_failure_text("设备命令查询", proposal_id, execution_result.message, "APPROVED"),
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
    if proposal.status == "UNKNOWN":
        return ToolResult(
            control="ok",
            content=(
                f"提案 {proposal_id} 执行结果不确定，需人工核实是否已执行或授权重试。"
            ),
        )
    if proposal.status == "REJECTED":
        return ToolResult(control="ok", content=f"提案 {proposal_id} 已被拒绝")
    if proposal.status == "PENDING":
        return ToolResult(
            control="ok",
            content=f"提案 {proposal_id} 正在等待人工审批，尚未执行。",
        )
    if proposal.status == "APPROVED":
        return ToolResult(
            control="ok",
            content=(
                f"提案 {proposal_id} 已批准但尚未执行成功，"
                "需管理员在审批卡片上触发执行或排查配置后重试。"
            ),
        )
    return ToolResult(
        control="ok",
        content=f"提案 {proposal_id} 当前状态：{proposal.status}",
    )


def _policy_label_for_mode(decision: str | None, approval_mode: str) -> str:
    """按会话审批档位生成命令策略的中文说明。

    Args:
        decision: 设备命令策略判定（blacklist / whitelist / None）。
        approval_mode: 会话审批档位（ask / assist / full）。

    Returns:
        给模型看的策略文案。
    """
    if decision == "blacklist":
        return "黑名单（禁止执行）"
    if decision == "whitelist":
        if approval_mode == "ask":
            return "白名单（当前为请求审批，需人工批准）"
        return "白名单（可自动执行）"
    if approval_mode == "full":
        return "未分类（完全访问，可自动执行）"
    return "未分类（需人工审批）"


async def list_device_commands_for_asset(
    db: AsyncSession,
    *,
    session_id: int,
    asset_id: int,
) -> ToolResult:
    """列出一台资产可用的诊断命令、审批策略与凭据前提。

    只读、无审批要求；让模型先查询"这台设备能做什么、要不要人工审批"，
    而不是靠猜命令名反复失败。

    Args:
        db: 当前事务使用的异步数据库会话。
        session_id: 根 Agent 所属会话 ID。
        asset_id: 目标 CMDB 资产 ID。

    Returns:
        含命令名、说明、审批策略与凭据状态的安全工具结果。
    """
    session = await agent_session_crud.get(db, session_id)
    if session is None:
        return ToolResult(control="rejected", content="会话不存在")

    approval_mode = session.approval_mode

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
        policy_label = _policy_label_for_mode(decision, approval_mode)
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
    lines.append(
        "变更类命令（reboot/shutdown/port_enable/port_disable）请用 device_control；"
        "只读诊断请用 query_device_command。"
    )

    return ToolResult(control="ok", content="\n".join(lines))
