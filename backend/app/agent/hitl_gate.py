"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl_gate.py
@DateTime: 2026-08-14
@Docs: HITL 门控钩子：before 建提案并统一执行，阻止 base dispatcher 重复执行。

实现流程：
1. run_loop 在 dispatch 前调用 before：门控工具先走与 tool_dispatch 相同的 Pydantic 校验。
2. 校验通过后 gate_action 在独立短会话中建提案；PENDING 返回 pending_approval 并 block。
3. 自动批准时 before 内调用 execute_approved_proposal，返回完整 ToolResult 并 block。
4. 非门控工具 before 立即放行；after 不再回写执行结果。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.hitl import (
    HitlEventPublisher,
    HitlProposalRejectedError,
    ProposalSafeSummary,
    gate_action,
)
from app.agent.hitl_execution import execute_approved_proposal
from app.agent.loop import BeforeToolDecision, ToolDispatcher, ToolResult

_GATED_TOOLS: frozenset[str] = frozenset({"notify", "device_control", "query_device_command"})

_PENDING_MESSAGES: dict[str, str] = {
    "notify": "通知提案",
    "device_control": "设备管控请求",
    "query_device_command": "设备命令查询",
}


def _pending_result(summary: ProposalSafeSummary, tool_name: str) -> ToolResult:
    """构造待人工审批的安全工具结果。"""
    label = _PENDING_MESSAGES.get(tool_name, "提案")
    return ToolResult(
        control="pending_approval",
        content=f"{label} {summary.proposal_id} 已创建，正在等待人工审批。",
    )


def _tool_result_from_summary(summary: ProposalSafeSummary, tool_name: str) -> ToolResult:
    """将执行服务返回的安全摘要映射为工具结果。"""
    label = _PENDING_MESSAGES.get(tool_name, "提案")
    proposal_id = summary.proposal_id

    if summary.status == "EXECUTED":
        if summary.action_type in ("device_query", "device_control"):
            excerpt = summary.result_excerpt or "（无输出）"
            return ToolResult(
                control="ok",
                content=f"{label} {proposal_id} 已自动批准并执行：\n{excerpt}",
            )
        return ToolResult(
            control="ok",
            content=f"{label} {proposal_id} 已自动批准并执行。",
        )

    if summary.status == "UNKNOWN":
        return ToolResult(
            control="failed",
            content=(
                f"{label} {proposal_id} 执行结果不确定，已标记为待人工核实。"
                "请管理员在审批卡片上确认是否已执行或授权重试。"
            ),
        )

    if summary.status == "REJECTED":
        return ToolResult(
            control="rejected",
            content=f"{label} {proposal_id} 已被拒绝（策略复检命中黑名单）。",
        )

    if summary.status == "APPROVED":
        return ToolResult(
            control="failed",
            content=(
                f"{label} {proposal_id} 已批准但未能开始执行，"
                "请检查命令、凭据或设备配置后由管理员重试。"
            ),
        )

    return ToolResult(
        control="failed",
        content=f"{label} {proposal_id} 当前状态为 {summary.status}，未完成执行。",
    )


class HitlGateHook:
    """根 Agent 循环的 HITL 门控钩子。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | AsyncSession,
        *,
        session_id: int,
        actor_user_id: int,
        proposed_by_agent_id: str | None = None,
        publisher: HitlEventPublisher | None = None,
    ) -> None:
        """绑定可信会话上下文、独立短会话工厂与可选事件发布器。"""
        if isinstance(session_factory, AsyncSession):
            engine = session_factory.bind
            if engine is None:
                raise ValueError("门控钩子需要绑定 AsyncEngine 的 AsyncSession")
            self._session_factory = async_sessionmaker(
                engine,
                expire_on_commit=False,
                autoflush=False,
            )
        else:
            self._session_factory = session_factory
        self._session_id = session_id
        self._actor_user_id = actor_user_id
        self._proposed_by_agent_id = proposed_by_agent_id
        self._publisher = publisher

    async def before(self, name: str, arguments: dict[str, Any]) -> BeforeToolDecision:
        """门控工具校验参数、建提案并统一执行；其它工具立即放行。"""
        if name not in _GATED_TOOLS:
            return BeforeToolDecision(block=False)

        from app.agent.tool_dispatch import (
            DeviceControlArgs,
            NotifyArgs,
            QueryDeviceCommandArgs,
            validation_reason_for_tool,
        )

        gate_models: dict[str, type] = {
            "notify": NotifyArgs,
            "device_control": DeviceControlArgs,
            "query_device_command": QueryDeviceCommandArgs,
        }

        argument_model = gate_models.get(name)
        if argument_model is None:
            return BeforeToolDecision(
                block=True,
                result=ToolResult(control="rejected", content=f"未知门控工具 {name!r}"),
            )

        try:
            parsed = argument_model.model_validate(arguments)
        except ValidationError as exc:
            return BeforeToolDecision(
                block=True,
                result=ToolResult(
                    control="clarification",
                    content=validation_reason_for_tool(name, exc),
                ),
            )

        action_type: str
        asset_id: int
        payload: dict[str, object]
        reason: str

        if isinstance(parsed, NotifyArgs):
            action_type = "notify"
            asset_id = parsed.asset_id
            payload = parsed.payload.model_dump()
            reason = parsed.reason
        elif isinstance(parsed, DeviceControlArgs):
            action_type = "device_control"
            asset_id = parsed.asset_id
            reason = parsed.reason
            payload = {"command_name": parsed.command_name}
            if parsed.interface_name is not None:
                payload["interface_name"] = parsed.interface_name
        elif isinstance(parsed, QueryDeviceCommandArgs):
            action_type = "device_query"
            asset_id = parsed.asset_id
            reason = parsed.reason
            payload = {"command_name": parsed.command_name}
        else:
            return BeforeToolDecision(
                block=True,
                result=ToolResult(control="failed", content=f"门控工具 {name!r} 参数模型未绑定"),
            )

        try:
            async with self._session_factory() as gate_db:
                summary = await gate_action(
                    gate_db,
                    session_id=self._session_id,
                    proposed_by_agent_id=self._proposed_by_agent_id,
                    action_type=action_type,
                    asset_id=asset_id,
                    payload=payload,
                    reason=reason,
                    actor_user_id=self._actor_user_id,
                    publisher=self._publisher,
                )
                await gate_db.commit()
        except HitlProposalRejectedError as exc:
            label = _PENDING_MESSAGES.get(name, "提案")
            return BeforeToolDecision(
                block=True,
                result=ToolResult(control="rejected", content=f"{label}被拒绝：{exc}"),
            )
        except Exception as exc:
            return BeforeToolDecision(
                block=True,
                result=ToolResult(
                    control="failed",
                    content=f"门控建提案失败：{type(exc).__name__}",
                ),
            )

        if summary.status == "PENDING":
            return BeforeToolDecision(block=True, result=_pending_result(summary, name))

        executed = await execute_approved_proposal(
            session_factory=self._session_factory,
            proposal_id=summary.proposal_id,
            actor_user_id=self._actor_user_id,
            publisher=self._publisher,
        )
        return BeforeToolDecision(
            block=True,
            result=_tool_result_from_summary(executed, name),
        )

    async def after(self, name: str, arguments: dict[str, Any], result: ToolResult) -> None:
        """门控工具执行已在 before 完成，after 无需回写。"""
        return None


async def dispatch_through_hitl_gate(
    gate: HitlGateHook,
    dispatch: ToolDispatcher,
    name: str,
    arguments: dict[str, Any],
) -> ToolResult:
    """按 run_loop 顺序执行：before → dispatch → after。"""
    decision = await gate.before(name, arguments)
    if decision.block:
        if decision.result is None:
            return ToolResult(control="failed", content="门控拦截但缺少工具结果")
        return decision.result
    tool_result = await dispatch(name, arguments)
    await gate.after(name, arguments, tool_result)
    return tool_result
