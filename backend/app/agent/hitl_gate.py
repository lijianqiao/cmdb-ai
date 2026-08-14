"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl_gate.py
@DateTime: 2026-08-14
@Docs: HITL 门控钩子：before 建提案/自动批准，after 回写自动批准执行结果。

实现流程：
1. run_loop 在 dispatch 前调用 before：门控工具先走与 tool_dispatch 相同的 Pydantic 校验。
2. 校验通过后 gate_action 建提案；需人工则 block 并返回 pending_approval，可自动批准则放行并记住 proposal_id。
3. 薄工具在放行后真执行 Scrapli/通知；after 对自动批准路径调用 attach_execution_result 回写。
4. 非门控工具（知识库/CMDB/监控等）before 立即放行，after 无操作。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.hitl import (
    HitlEventPublisher,
    HitlProposalRejectedError,
    attach_execution_result,
    gate_action,
)
from app.agent.loop import BeforeToolDecision, ToolDispatcher, ToolResult

_GATED_TOOLS: frozenset[str] = frozenset({"notify", "device_control", "query_device_command"})

_PENDING_MESSAGES: dict[str, str] = {
    "notify": "通知提案",
    "device_control": "设备管控请求",
    "query_device_command": "设备命令查询",
}


class HitlGateHook:
    """根 Agent 循环的 HITL 门控钩子。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        session_id: int,
        actor_user_id: int,
        proposed_by_agent_id: str | None = None,
        publisher: HitlEventPublisher | None = None,
    ) -> None:
        """绑定可信会话上下文与可选事件发布器。"""
        self._db = db
        self._session_id = session_id
        self._actor_user_id = actor_user_id
        self._proposed_by_agent_id = proposed_by_agent_id
        self._publisher = publisher
        self._proposal_id: int | None = None
        self._auto_approved = False

    @property
    def current_proposal_id(self) -> int | None:
        """当前放行轮次关联的提案 ID（仅自动批准路径有值）。"""
        return self._proposal_id

    async def before(self, name: str, arguments: dict[str, Any]) -> BeforeToolDecision:
        """门控工具校验参数并建提案；其它工具立即放行。"""
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
            summary = await gate_action(
                self._db,
                session_id=self._session_id,
                proposed_by_agent_id=self._proposed_by_agent_id,
                action_type=action_type,
                asset_id=asset_id,
                payload=payload,
                reason=reason,
                actor_user_id=self._actor_user_id,
                publisher=self._publisher,
            )
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
            self._proposal_id = None
            self._auto_approved = False
            label = _PENDING_MESSAGES.get(name, "提案")
            return BeforeToolDecision(
                block=True,
                result=ToolResult(
                    control="pending_approval",
                    content=f"{label} {summary.proposal_id} 已创建，正在等待人工审批。",
                ),
            )

        if summary.status == "APPROVED":
            self._proposal_id = summary.proposal_id
            self._auto_approved = True
            return BeforeToolDecision(block=False)

        self._proposal_id = None
        self._auto_approved = False
        label = _PENDING_MESSAGES.get(name, "提案")
        return BeforeToolDecision(
            block=True,
            result=ToolResult(
                control="failed",
                content=f"{label} {summary.proposal_id} 当前状态为 {summary.status}，未完成执行。",
            ),
        )

    async def after(self, name: str, arguments: dict[str, Any], result: ToolResult) -> None:
        """自动批准路径将薄工具结果写回提案。"""
        if name not in _GATED_TOOLS or not self._auto_approved or self._proposal_id is None:
            return
        await attach_execution_result(
            self._db,
            proposal_id=self._proposal_id,
            tool_result=result,
            actor_user_id=self._actor_user_id,
            publisher=self._publisher,
        )
        self._proposal_id = None
        self._auto_approved = False


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
