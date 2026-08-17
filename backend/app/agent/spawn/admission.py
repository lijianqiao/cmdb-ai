"""Spawn 准入校验：预算合法性与路径深度。

纯函数，不持有状态也不访问数据库。SpawnManager 把自己的上限作为参数传进来，
这样「限额是多少」由管理器决定，「怎么算越界」由本模块决定，两者可以分开测试。
"""

import math

from app.agent.spawn.types import (
    _MAX_CHILD_STEP,
    ChildBudgetSnapshot,
    SpawnRejectedError,
)


def validate_child_budget(
    override: ChildBudgetSnapshot | None,
    *,
    max_steps: int,
    max_cost_usd: float,
    max_wall_time_seconds: float,
) -> ChildBudgetSnapshot:
    """校验并归一化一个子 Agent 预算，越界一律拒绝而不是截断。

    Args:
        override: 调用方给出的预算；None 表示用下面三个上限作为默认值。
        max_steps: 单个子 Agent 允许的最大步数。
        max_cost_usd: 单个子 Agent 允许的最大成本。
        max_wall_time_seconds: 单个子 Agent 允许的最大墙钟时间。

    Returns:
        已归一化的预算快照，用量字段强制归零。

    Raises:
        SpawnRejectedError: 任一字段非法或越界；limit_name 指出是哪一项。
    """
    candidate = override or ChildBudgetSnapshot(
        max_steps=max_steps,
        max_cost_usd=max_cost_usd,
        max_wall_time_seconds=max_wall_time_seconds,
    )
    if (
        isinstance(candidate.max_steps, bool)
        or not isinstance(candidate.max_steps, int)
        or not 1 <= candidate.max_steps <= max_steps
        or candidate.max_steps > _MAX_CHILD_STEP
    ):
        raise SpawnRejectedError(
            "invalid_child_budget", limit_name="child_max_steps"
        )
    if (
        isinstance(candidate.max_cost_usd, bool)
        or not isinstance(candidate.max_cost_usd, int | float)
        or not math.isfinite(candidate.max_cost_usd)
        or not 0 <= candidate.max_cost_usd <= max_cost_usd
    ):
        raise SpawnRejectedError(
            "invalid_child_budget", limit_name="child_max_cost_usd"
        )
    if (
        isinstance(candidate.max_wall_time_seconds, bool)
        or not isinstance(candidate.max_wall_time_seconds, int | float)
        or not math.isfinite(candidate.max_wall_time_seconds)
        or not 0
        < candidate.max_wall_time_seconds
        <= max_wall_time_seconds
    ):
        raise SpawnRejectedError(
            "invalid_child_budget", limit_name="child_max_wall_time_seconds"
        )
    if (
        isinstance(candidate.steps_used, bool)
        or not isinstance(candidate.steps_used, int)
        or candidate.steps_used != 0
    ):
        raise SpawnRejectedError(
            "invalid_child_budget", limit_name="steps_used"
        )
    if (
        isinstance(candidate.cost_used_usd, bool)
        or not isinstance(candidate.cost_used_usd, int | float)
        or not math.isfinite(candidate.cost_used_usd)
        or candidate.cost_used_usd != 0
    ):
        raise SpawnRejectedError(
            "invalid_child_budget", limit_name="cost_used_usd"
        )
    return ChildBudgetSnapshot(
        max_steps=candidate.max_steps,
        max_cost_usd=float(candidate.max_cost_usd),
        max_wall_time_seconds=float(candidate.max_wall_time_seconds),
    )


def depth_from_path(agent_path: str) -> int:
    """按父 agent_path 推算子 Agent 的深度；路径不以 root 开头视为非法。"""
    parts = [part for part in agent_path.split("/") if part]
    if not parts or parts[0] != "root":
        raise SpawnRejectedError("invalid_parent_path")
    return len(parts) - 1


def path_depth(agent_path: str) -> int:
    """统计路径段数。关闭时按深度倒序处理，保证先关最深的后代。"""
    return len([part for part in agent_path.split("/") if part])
