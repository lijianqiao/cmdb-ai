"""The standard agent loop (docs/guide.md §2.1).

Call the model, dispatch every requested tool call, append results, repeat
until the model returns a final answer (no tool_calls), a tool signals an
early-exit control, or the budget is exhausted. The model decides *what* to
call; whether the call is allowed is decided entirely inside `dispatch_tool`
(docs/guide.md §3.1) — this loop never inspects tool arguments itself.

Only `pending_approval` ends the turn early（等待人工审批）。`clarification` /
`rejected` / `failed` 的工具结果会回灌给模型继续循环，让它修正参数或向用户
解释原因；连续多轮全部失败才强制退出，防止小模型死循环烧预算。

不在等模型、等工具时占数据库连接（R5）：每一步用短会话读历史（读完即还连接），
本轮新产生的 assistant/tool 消息留在内存里、与已提交的历史拼成模型窗口，循环
结束时经 ``persist_messages`` 一次写库。循环因异常或取消中途退出时这些消息直接
丢弃，与原来「整轮一个事务、出错回滚」的语义一致；绝不每条消息各自提交。
"""

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Literal

from app.agent.budget import Budget, BudgetExceededError
from app.agent.compaction import ensure_root_compaction
from app.agent.session import build_model_history, persist_transcript
from app.agent.transcript import TranscriptMessage
from app.core.database import SessionSource
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


@dataclass(frozen=True, slots=True)
class BeforeToolDecision:
    """Whether a tool call should be blocked before dispatch."""

    block: bool
    result: ToolResult | None = None


type ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]
type ChatFn = Callable[..., Awaitable[ChatResult]]
type BeforeToolCall = Callable[[str, dict[str, Any]], Awaitable[BeforeToolDecision]]
type AfterToolCall = Callable[[str, dict[str, Any], ToolResult], Awaitable[None]]
# 把本轮攒下的消息按顺序写库，返回 messages[usage_index] 的主键（没有则 None）。
type PersistMessages = Callable[
    [Sequence[TranscriptMessage], int | None], Awaitable[int | None]
]


async def _default_before_tool_call(name: str, arguments: dict[str, Any]) -> BeforeToolDecision:
    return BeforeToolDecision(block=False)


async def _default_after_tool_call(
    name: str, arguments: dict[str, Any], result: ToolResult
) -> None:
    pass


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """Why the loop stopped, and its final text if it produced one.

    `cancelled` 不由 run_loop 自己产生——它是用户主动中止时由 API 层构造的，
    放在同一个枚举里是为了让 AgentChatTurnResponse 只有一个 reason 字段。
    """

    reason: Literal[
        "final_answer", "budget_exceeded", "early_exit", "llm_error", "cancelled"
    ]
    final_answer: str | None
    control: ToolControl | None = None
    # 本轮最后写入的那条 assistant 消息的主键，整轮用量回写到这一行上。
    # 写它的时刻子 Agent 的账还没并进来，所以只能先把 id 带出去由调用方补写。
    usage_message_id: int | None = None


def _parse_arguments(tool_call: ToolCall) -> dict[str, Any]:
    """Parse a tool call's JSON argument string, never raising into the loop."""
    try:
        parsed = json.loads(tool_call.arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def run_loop(
    db: SessionSource,
    *,
    session_id: int,
    model_key: str,
    dispatch_tool: ToolDispatcher,
    tools: list[dict[str, Any]] | None = None,
    budget: Budget | None = None,
    chat_fn: ChatFn = chat,
    agent_id: str | None = None,
    system_prompt: str | None = None,
    before_tool_call: BeforeToolCall | None = None,
    after_tool_call: AfterToolCall | None = None,
    persist_messages: PersistMessages | None = None,
) -> LoopOutcome:
    """Run one standard agent loop turn against `session_id`'s transcript.

    Args:
        db: 会话工厂（生产：每段读写一个短会话，等模型/工具时不占连接）或借用的会话（测试）
        persist_messages: 循环结束时写入本轮消息的方式；默认在一个短事务里按顺序写入
            （借用的会话只 flush，由调用方提交）
    """
    active_budget = budget or Budget()
    consecutive_failed_rounds = 0
    before_hook = before_tool_call or _default_before_tool_call
    after_hook = after_tool_call or _default_after_tool_call
    persist = persist_messages or partial(persist_transcript, db, session_id, agent_id=agent_id)
    last_prompt_tokens: int | None = None
    pending: list[TranscriptMessage] = []

    async def finish(outcome: LoopOutcome, *, usage_index: int | None = None) -> LoopOutcome:
        usage_message_id = await persist(pending, usage_index)
        return replace(outcome, usage_message_id=usage_message_id)

    while True:
        try:
            active_budget.reserve_step()
        except BudgetExceededError:
            return await finish(LoopOutcome(reason="budget_exceeded", final_answer=None))

        if agent_id is None:
            await ensure_root_compaction(
                db,
                session_id,
                budget=active_budget,
                system_prompt=system_prompt or "",
                last_prompt_tokens=last_prompt_tokens,
                pending=pending,
            )

        history = await build_model_history(
            db,
            session_id,
            agent_id=agent_id,
            system_prompt=system_prompt,
            pending=pending,
        )
        result: ChatResult = (
            await chat_fn(model_key, history, tools=tools, db=db)
            if chat_fn is chat
            else await chat_fn(model_key, history, tools=tools)
        )

        if result.finish_reason == "error":
            return await finish(LoopOutcome(reason="llm_error", final_answer=None))

        last_prompt_tokens = result.prompt_tokens

        cost_exceeded = False
        try:
            active_budget.record_cost(
                result.cost_usd,
                # 用 ChatResult 回传的**生效**键，不是请求时传入的 model_key：
                # 请求便宜档而那一档没配时实际跑的是平衡档，用量要如实归到平衡档
                model_key=result.model_key or model_key,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )
        except BudgetExceededError:
            cost_exceeded = True

        if not result.tool_calls:
            pending.append(TranscriptMessage.assistant(result.content or ""))
            return await finish(
                LoopOutcome(reason="final_answer", final_answer=result.content),
                usage_index=len(pending) - 1,
            )

        if cost_exceeded:
            return await finish(LoopOutcome(reason="budget_exceeded", final_answer=None))

        pending_tool_results: list[tuple[ToolCall, ToolResult]] = []
        round_controls: list[ToolControl] = []
        for index, tool_call in enumerate(result.tool_calls):
            arguments = _parse_arguments(tool_call)
            decision = await before_hook(tool_call.name, arguments)
            if decision.block:
                if decision.result is None:
                    raise ValueError("block=True 时 result 必填")
                tool_result = decision.result
            else:
                tool_result = await dispatch_tool(tool_call.name, arguments)
                await after_hook(tool_call.name, arguments, tool_result)
            round_controls.append(tool_result.control)
            pending_tool_results.append((tool_call, tool_result))
            if tool_result.control in _EARLY_EXIT_CONTROLS:
                for skipped_call in result.tool_calls[index + 1 :]:
                    pending_tool_results.append(
                        (
                            skipped_call,
                            ToolResult(
                                control="ok",
                                content="已跳过：等待前一个工具调用的处理结果",
                            ),
                        )
                    )
                pending.append(
                    TranscriptMessage.assistant(result.content or "", result.tool_calls)
                )
                exit_index = len(pending) - 1
                pending.extend(
                    TranscriptMessage.tool_result(paired_call.id, paired_result.content)
                    for paired_call, paired_result in pending_tool_results
                )
                return await finish(
                    LoopOutcome(
                        reason="early_exit",
                        final_answer=None,
                        control=tool_result.control,
                    ),
                    usage_index=exit_index,
                )

        pending.append(TranscriptMessage.assistant(result.content or "", result.tool_calls))
        pending.extend(
            TranscriptMessage.tool_result(paired_call.id, paired_result.content)
            for paired_call, paired_result in pending_tool_results
        )

        # 整轮工具全部失败才累计；有任何一次成功就重置，避免误杀正常纠错。
        if round_controls and all(control in _FAILURE_CONTROLS for control in round_controls):
            consecutive_failed_rounds += 1
            if consecutive_failed_rounds >= _MAX_CONSECUTIVE_FAILED_ROUNDS:
                return await finish(
                    LoopOutcome(reason="early_exit", final_answer=None, control=round_controls[-1])
                )
        else:
            consecutive_failed_rounds = 0
