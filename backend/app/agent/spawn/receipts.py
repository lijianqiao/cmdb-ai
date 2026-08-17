"""ORM 注册行 ↔ 不可变回执的转换与完整性校验。

全部是纯函数：不碰数据库会话、不碰 asyncio、不读配置。持久化的 budget 是 JSON 列，
任何字段缺失或类型异常都必须在转成回执时就炸出 ChildReceiptCorruptionError，
而不是等到下游拿着半个回执做判断。
"""

import math
from datetime import UTC, datetime

from app.agent.spawn.types import (
    _MAX_CHILD_STEP,
    ChildBudgetSnapshot,
    ChildReceipt,
    ChildReceiptCorruptionError,
)
from app.models.agent_registry import AgentRegistry


def _receipt_step(value: object, *, child_id: str, field: str, minimum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_CHILD_STEP
    ):
        raise ChildReceiptCorruptionError(child_id, field=field)
    return value


def _receipt_number(
    value: object,
    *,
    child_id: str,
    field: str,
    positive: bool,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ChildReceiptCorruptionError(child_id, field=field)
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ChildReceiptCorruptionError(child_id, field=field) from None
    if not math.isfinite(number) or (number <= 0 if positive else number < 0):
        raise ChildReceiptCorruptionError(child_id, field=field)
    return number


def _receipt_strings(value: object, *, child_id: str, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ChildReceiptCorruptionError(child_id, field=field)
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ChildReceiptCorruptionError(child_id, field=field)
        strings.append(item)
    return tuple(strings)


def _utc(value: datetime | None) -> datetime | None:
    """Normalize SQLite's naive round-trip and timezone-aware DB values alike."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

def _to_receipt(row: AgentRegistry) -> ChildReceipt:
    """Copy one ORM row into immutable values without retaining session state."""
    if not isinstance(row.budget, dict):
        raise ChildReceiptCorruptionError(row.child_id, field="budget")
    budget = row.budget
    budget_fields = (
        "max_steps",
        "max_cost_usd",
        "max_wall_time_seconds",
        "steps_used",
        "cost_used_usd",
    )
    for field in budget_fields:
        if field not in budget:
            raise ChildReceiptCorruptionError(
                row.child_id,
                field=f"budget.{field}",
            )
    steps_used = _receipt_step(
        budget["steps_used"],
        child_id=row.child_id,
        field="budget.steps_used",
        minimum=0,
    )
    max_steps = _receipt_step(
        budget["max_steps"],
        child_id=row.child_id,
        field="budget.max_steps",
        minimum=1,
    )
    if steps_used > max_steps:
        raise ChildReceiptCorruptionError(
            row.child_id,
            field="budget.steps_used",
        )
    return ChildReceipt(
        child_id=row.child_id,
        trace_id=row.trace_id,
        session_id=row.session_id,
        parent_agent_id=row.parent_agent_id,
        agent_path=row.agent_path,
        role=row.role,
        role_version=row.role_version,
        model=row.model,
        tools_allowlist=_receipt_strings(
            row.tools_allowlist,
            child_id=row.child_id,
            field="tools_allowlist",
        ),
        sandbox_mode=row.sandbox_mode,
        task_brief=row.task_brief,
        budget=ChildBudgetSnapshot(
            max_steps=max_steps,
            max_cost_usd=_receipt_number(
                budget["max_cost_usd"],
                child_id=row.child_id,
                field="budget.max_cost_usd",
                positive=False,
            ),
            max_wall_time_seconds=_receipt_number(
                budget["max_wall_time_seconds"],
                child_id=row.child_id,
                field="budget.max_wall_time_seconds",
                positive=True,
            ),
            steps_used=steps_used,
            cost_used_usd=_receipt_number(
                budget["cost_used_usd"],
                child_id=row.child_id,
                field="budget.cost_used_usd",
                positive=False,
            ),
            # token 两项是后加的，用 .get 兜底：旧的在途行没有这两个键，
            # 把它们列进上面的必填校验会让那些行直接判成损坏
            prompt_tokens_used=_receipt_step(
                budget.get("prompt_tokens_used", 0),
                child_id=row.child_id,
                field="budget.prompt_tokens_used",
                minimum=0,
            ),
            completion_tokens_used=_receipt_step(
                budget.get("completion_tokens_used", 0),
                child_id=row.child_id,
                field="budget.completion_tokens_used",
                minimum=0,
            ),
        ),
        status=row.status,
        result_summary=row.result_summary,
        artifacts=_receipt_strings(
            row.artifacts,
            child_id=row.child_id,
            field="artifacts",
        ),
        created_at=_utc(row.created_at),  # type: ignore[arg-type]
        status_changed_at=_utc(row.status_changed_at),  # type: ignore[arg-type]
        closed_at=_utc(row.closed_at),
        force_closed=row.force_closed,
    )


def _budget_payload(snapshot: ChildBudgetSnapshot) -> dict[str, object]:
    return {
        "max_steps": snapshot.max_steps,
        "max_cost_usd": snapshot.max_cost_usd,
        "max_wall_time_seconds": snapshot.max_wall_time_seconds,
        "steps_used": snapshot.steps_used,
        "cost_used_usd": snapshot.cost_used_usd,
        "prompt_tokens_used": snapshot.prompt_tokens_used,
        "completion_tokens_used": snapshot.completion_tokens_used,
    }
