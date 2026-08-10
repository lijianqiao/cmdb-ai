"""The standard agent loop (docs/guide.md §2.1).

Call the model, dispatch every requested tool call, append results, repeat
until the model returns a final answer (no tool_calls), a tool signals an
early-exit control, or the budget is exhausted. The model decides *what* to
call; whether the call is allowed is decided entirely inside `dispatch_tool`
(docs/guide.md §3.1) — this loop never inspects tool arguments itself.
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

_EARLY_EXIT_CONTROLS: frozenset[ToolControl] = frozenset({"clarification", "pending_approval", "rejected"})


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
) -> LoopOutcome:
    """Run one standard agent loop turn against `session_id`'s transcript."""
    active_budget = budget or Budget()

    while True:
        try:
            active_budget.record_step()
        except BudgetExceededError:
            return LoopOutcome(reason="budget_exceeded", final_answer=None)

        history = await build_model_history(db, session_id)
        result: ChatResult = await chat_fn(model_key, history, tools=tools)

        if not result.tool_calls:
            await append_assistant_message(db, session_id, result.content or "")
            return LoopOutcome(reason="final_answer", final_answer=result.content)

        await append_assistant_message(
            db, session_id, result.content or "", tool_calls=result.tool_calls
        )

        for tool_call in result.tool_calls:
            tool_result = await dispatch_tool(tool_call.name, _parse_arguments(tool_call))
            await append_tool_result(db, session_id, tool_call.id, tool_result.content)
            if tool_result.control in _EARLY_EXIT_CONTROLS:
                return LoopOutcome(
                    reason="early_exit", final_answer=None, control=tool_result.control
                )
