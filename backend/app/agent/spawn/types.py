"""子 Agent Spawn 的数据契约：状态常量、回执结构、异常与事件协议。

本模块是 spawn 包的依赖底座，**不 import 包内其它模块**，只依赖 budget 与 SQLAlchemy 类型。
把这些定义单独拿出来，是为了让 receipts / admission / manager 三者共享同一套契约而不互相依赖。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import Budget

type ChildTerminalStatus = Literal["COMPLETED", "FAILED"]
type ChildErrorClass = Literal["model", "tool", "policy_reject", "infra", "budget_exceeded"]

_ACTIVE_STATUSES = frozenset({"REQUESTED", "SPAWNING", "RUNNING"})
_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "CLOSED"})
_SAFE_ERROR_CLASSES = frozenset(
    {"model", "tool", "policy_reject", "infra", "budget_exceeded"}
)
# AgentTraceEvent.step uses PostgreSQL's signed 32-bit Integer; close adds one.
_MAX_CHILD_STEP = 2_147_483_646

class _ChildToolRuntimeError(RuntimeError):
    """Marks an unexpected exception that escaped the fail-closed dispatcher."""


@dataclass(frozen=True, slots=True)
class ChildBudgetSnapshot:
    """Immutable configured limits plus the latest persisted usage."""

    max_steps: int
    max_cost_usd: float
    max_wall_time_seconds: float
    steps_used: int = 0
    cost_used_usd: float = 0.0
    # 子 Agent 的 token 用量要能并进父轮次的合计里，否则界面上一次多路排查
    # 只显示根循环那点开销，会严重低估
    prompt_tokens_used: int = 0
    completion_tokens_used: int = 0


@dataclass(frozen=True, slots=True)
class ChildReceipt:
    """Session-independent, immutable view of one durable registry row."""

    child_id: str
    trace_id: str
    session_id: int
    parent_agent_id: str | None
    agent_path: str
    role: str
    role_version: str
    model: str
    tools_allowlist: tuple[str, ...]
    sandbox_mode: str
    task_brief: str
    budget: ChildBudgetSnapshot
    status: str
    result_summary: str | None
    artifacts: tuple[str, ...]
    created_at: datetime
    status_changed_at: datetime
    closed_at: datetime | None
    force_closed: bool


@dataclass(frozen=True, slots=True)
class ChildRunResult:
    """Safe result returned by an injected or default child runner."""

    status: ChildTerminalStatus
    result_summary: str | None
    artifacts: tuple[str, ...] = ()
    error_class: ChildErrorClass | None = None


type ChildRunner = Callable[
    [AsyncSession, ChildReceipt, Budget], Awaitable[ChildRunResult]
]

class SpawnRejectedError(ValueError):
    """A request failed preflight without creating a registry row."""

    def __init__(self, reason: str, *, limit_name: str | None = None) -> None:
        self.reason = reason
        self.limit_name = limit_name
        super().__init__(f"child spawn rejected: {reason}")


class ChildNotFoundError(LookupError):
    """The opaque child id has no durable receipt."""

    def __init__(self, child_id: str) -> None:
        self.child_id = child_id
        super().__init__("child receipt not found")


class ChildWaitTimeoutError(TimeoutError):
    """A wait deadline expired while child execution continues."""

    def __init__(self, child_id: str, timeout_ms: int) -> None:
        self.child_id = child_id
        self.timeout_ms = timeout_ms
        super().__init__("child wait deadline expired")


class ChildRuntimeUnavailableError(RuntimeError):
    """A durable active row has no runnable task in this process."""

    def __init__(self, child_id: str, *, reason: str = "runtime_unavailable") -> None:
        self.child_id = child_id
        self.reason = reason
        super().__init__(f"child runtime unavailable: {reason}")


class ChildReceiptCorruptionError(RuntimeError):
    """A durable registry row cannot be converted into a safe receipt."""

    def __init__(self, child_id: str, *, field: str) -> None:
        self.child_id = child_id
        self.field = field
        super().__init__(f"child receipt is corrupt: {field}")

@runtime_checkable
class SpawnEventPublisher(Protocol):
    """子 Agent 生命周期事件发布协议。"""

    async def publish_child_status(self, receipt: ChildReceipt) -> None:
        """在持久化状态提交后广播安全子任务摘要。"""
        ...


class NoopSpawnEventPublisher:
    """默认空实现：测试与未注入 WS 时不广播。"""

    async def publish_child_status(self, receipt: ChildReceipt) -> None:
        del receipt


@dataclass(slots=True)
class _SessionRuntime:
    lock: asyncio.Lock
    slots: asyncio.BoundedSemaphore
    held_child_ids: set[str]
    closing_child_counts: dict[str, int]
