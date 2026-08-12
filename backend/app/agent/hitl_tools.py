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

from app.agent.hitl import (
    ActionType,
    HitlEventPublisher,
    HitlProposalRejectedError,
    propose_action,
)
from app.agent.loop import ToolResult


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
        content=f"整改提案 {summary.proposal_id} 当前状态为 {summary.status}，未完成执行。",
    )
