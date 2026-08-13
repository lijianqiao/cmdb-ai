"""The standard agent loop (docs/guide.md §2.1).

Call the model, dispatch every requested tool call, append results, repeat
until the model returns a final answer (no tool_calls), a tool signals an
early-exit control, or the budget is exhausted. The model decides *what* to
call; whether the call is allowed is decided entirely inside `dispatch_tool`
(docs/guide.md §3.1) — this loop never inspects tool arguments itself.

Only `pending_approval` ends the turn early（等待人工审批）。`clarification` /
`rejected` / `failed` 的工具结果会回灌给模型继续循环，让它修正参数或向用户
解释原因；连续多轮全部失败才强制退出，防止小模型死循环烧预算。
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import Budget, BudgetExceededError
from app.agent.session import append_assistant_message, append_tool_result, build_model_history
from app.core.llm import ChatResult, ToolCall, chat

type ToolControl = Literal["ok", "rejected", "failed", "clarification", "pending_approval"]

_EARLY_EXIT_CONTROLS: frozenset[ToolControl] = frozenset({"pending_approval"})
_FAILURE_CONTROLS: frozenset[ToolControl] = frozenset({"clarification", "rejected", "failed"})
_MAX_CONSECUTIVE_FAILED_ROUNDS = 3


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The structured result every tool dispatch must return (docs/guide.md §2.3)."""

    control: ToolControl
    content: str


type ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]
type ChatFn = Callable[..., Awaitable[ChatResult]]


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """Why the loop stopped, and its final text if it produced one."""

    reason: Literal["final_answer", "budget_exceeded", "early_exit"]
    final_answer: str | None
    control: ToolControl | None = None


def _parse_arguments(tool_call: ToolCall) -> dict[str, Any]:
    """Parse a tool call's JSON argument string, never raising into the loop."""
    try:
        parsed = json.loads(tool_call.arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def run_loop(
    db: AsyncSession,
    *,
    session_id: int,
    model_key: str,
    dispatch_tool: ToolDispatcher,
    tools: list[dict[str, Any]] | None = None,
    budget: Budget | None = None,
    chat_fn: ChatFn = chat,
    agent_id: str | None = None,
    system_prompt: str | None = None,
) -> LoopOutcome:
    """Run one standard agent loop turn against `session_id`'s transcript."""
    active_budget = budget or Budget()
    consecutive_failed_rounds = 0

    while True:
        try:
            active_budget.reserve_step()
        except BudgetExceededError:
            return LoopOutcome(reason="budget_exceeded", final_answer=None)

        history = await build_model_history(
            db,
            session_id,
            agent_id=agent_id,
            system_prompt=system_prompt,
        )
        result: ChatResult = (
            await chat_fn(model_key, history, tools=tools, db=db)
            if chat_fn is chat
            else await chat_fn(model_key, history, tools=tools)
        )

        cost_exceeded = False
        try:
            active_budget.record_cost(result.cost_usd)
        except BudgetExceededError:
            cost_exceeded = True

        if not result.tool_calls:
            await append_assistant_message(
                db, session_id, result.content or "", agent_id=agent_id
            )
            return LoopOutcome(reason="final_answer", final_answer=result.content)

        if cost_exceeded:
            return LoopOutcome(reason="budget_exceeded", final_answer=None)

        await append_assistant_message(
            db,
            session_id,
            result.content or "",
            agent_id=agent_id,
            tool_calls=result.tool_calls,
        )

        round_controls: list[ToolControl] = []
        for index, tool_call in enumerate(result.tool_calls):
            tool_result = await dispatch_tool(tool_call.name, _parse_arguments(tool_call))
            round_controls.append(tool_result.control)
            await append_tool_result(
                db, session_id, tool_call.id, tool_result.content, agent_id=agent_id
            )
            if tool_result.control in _EARLY_EXIT_CONTROLS:
                for skipped_call in result.tool_calls[index + 1 :]:
                    await append_tool_result(
                        db,
                        session_id,
                        skipped_call.id,
                        "已跳过：等待前一个工具调用的处理结果",
                        agent_id=agent_id,
                    )
                return LoopOutcome(
                    reason="early_exit", final_answer=None, control=tool_result.control
                )

        # 整轮工具全部失败才累计；有任何一次成功就重置，避免误杀正常纠错。
        if round_controls and all(control in _FAILURE_CONTROLS for control in round_controls):
            consecutive_failed_rounds += 1
            if consecutive_failed_rounds >= _MAX_CONSECUTIVE_FAILED_ROUNDS:
                return LoopOutcome(
                    reason="early_exit", final_answer=None, control=round_controls[-1]
                )
        else:
            consecutive_failed_rounds = 0
