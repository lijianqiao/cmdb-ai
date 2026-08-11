"""进程内子 Agent Spawn 运行时。

实现流程：
1. Spawn 前在同一 session 锁内完成角色、父子关系、预算和累计配额校验，非法请求不留回执。
2. 合法请求先持久化 REQUESTED/SPAWNING、独立 child 消息与 spawn trace，再创建进程内 task。
3. 每个 child 使用独立数据库 session 运行受限 Agent loop，并把预算用量和固定失败分类写回终态。
4. wait 只等待而不取消 child；send 只写 RUNNING child 的隔离消息空间；list 始终从 registry 重建快照。
5. close 按后代优先取消 task，有限等待后可强制 detach，并在 session 锁内只释放一次并发槽。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.budget import Budget
from app.agent.loop import ChatFn, ToolResult, run_loop
from app.agent.roles import get_role
from app.agent.session import append_user_message
from app.agent.tool_dispatch import build_tool_dispatcher, tool_schemas_for
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.llm import MODELS, LlmRequestError, chat
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.models.agent_registry import AgentRegistry
from app.models.agent_session import AgentSession

type ChildTerminalStatus = Literal["COMPLETED", "FAILED"]
type ChildErrorClass = Literal["model", "tool", "policy_reject", "infra"]

_ACTIVE_STATUSES = frozenset({"REQUESTED", "SPAWNING", "RUNNING"})
_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "CLOSED"})
_SAFE_ERROR_CLASSES = frozenset({"model", "tool", "policy_reject", "infra"})
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


@dataclass(slots=True)
class _SessionRuntime:
    lock: asyncio.Lock
    slots: asyncio.BoundedSemaphore
    held_child_ids: set[str]
    closing_child_counts: dict[str, int]


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


def _create_child_task(
    coroutine: Coroutine[Any, Any, None], *, name: str
) -> asyncio.Task[None]:
    """Narrow task-construction seam used to verify post-commit compensation."""
    return asyncio.create_task(coroutine, name=name)


async def _await_reconciliation[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Finish one cleanup task before propagating any caller cancellation."""
    task = asyncio.create_task(coroutine)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


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
    }


class SpawnManager:
    """Own child tasks and local concurrency slots for one Python process."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        child_runner: ChildRunner | None = None,
        *,
        chat_fn: ChatFn = chat,
        max_concurrent_children: int = settings.AGENT_MAX_CONCURRENT_CHILDREN,
        max_spawn_depth: int = settings.AGENT_MAX_SPAWN_DEPTH,
        max_children_per_session: int = settings.AGENT_MAX_CHILDREN_PER_SESSION,
        max_total_child_cost_usd: float = settings.AGENT_MAX_TOTAL_CHILD_COST_USD,
        child_max_steps: int = settings.AGENT_CHILD_MAX_STEPS,
        child_max_cost_usd: float = settings.AGENT_CHILD_MAX_COST_USD,
        child_max_wall_time_seconds: float = settings.AGENT_CHILD_MAX_WALL_TIME_SECONDS,
        close_timeout_seconds: float = settings.AGENT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._child_runner = child_runner
        self._chat_fn = chat_fn
        self._max_concurrent_children = max_concurrent_children
        self._max_spawn_depth = max_spawn_depth
        self._max_children_per_session = max_children_per_session
        self._max_total_child_cost_usd = max_total_child_cost_usd
        self._child_max_steps = child_max_steps
        self._child_max_cost_usd = child_max_cost_usd
        self._child_max_wall_time_seconds = child_max_wall_time_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._session_runtimes: dict[int, _SessionRuntime] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._active_budgets: dict[str, Budget] = {}

    @property
    def max_concurrent_children(self) -> int:
        """Maximum wave size available to workflow orchestration."""
        return self._max_concurrent_children

    def _runtime(self, session_id: int) -> _SessionRuntime:
        runtime = self._session_runtimes.get(session_id)
        if runtime is None:
            runtime = _SessionRuntime(
                lock=asyncio.Lock(),
                slots=asyncio.BoundedSemaphore(self._max_concurrent_children),
                held_child_ids=set(),
                closing_child_counts={},
            )
            self._session_runtimes[session_id] = runtime
        return runtime

    def _validated_budget(
        self, override: ChildBudgetSnapshot | None
    ) -> ChildBudgetSnapshot:
        candidate = override or ChildBudgetSnapshot(
            max_steps=self._child_max_steps,
            max_cost_usd=self._child_max_cost_usd,
            max_wall_time_seconds=self._child_max_wall_time_seconds,
        )
        if (
            isinstance(candidate.max_steps, bool)
            or not isinstance(candidate.max_steps, int)
            or not 1 <= candidate.max_steps <= self._child_max_steps
            or candidate.max_steps > _MAX_CHILD_STEP
        ):
            raise SpawnRejectedError(
                "invalid_child_budget", limit_name="child_max_steps"
            )
        if (
            isinstance(candidate.max_cost_usd, bool)
            or not isinstance(candidate.max_cost_usd, int | float)
            or not math.isfinite(candidate.max_cost_usd)
            or not 0 <= candidate.max_cost_usd <= self._child_max_cost_usd
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
            <= self._child_max_wall_time_seconds
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

    @staticmethod
    def _depth_from_path(agent_path: str) -> int:
        parts = [part for part in agent_path.split("/") if part]
        if not parts or parts[0] != "root":
            raise SpawnRejectedError("invalid_parent_path")
        return len(parts) - 1

    async def spawn_agent(
        self,
        *,
        session_id: int,
        role: str,
        task_brief: str,
        trace_id: str | None = None,
        parent_agent_id: str | None = None,
        model: str | None = None,
        tools_allowlist: Iterable[str] | None = None,
        budget: ChildBudgetSnapshot | None = None,
        fork_mode: str = "none",
    ) -> ChildReceipt:
        """Validate, reserve, persist, and start one child without queueing."""
        runtime = self._runtime(session_id)
        create_task_failed = False
        receipt: ChildReceipt | None = None
        child_id: str | None = None
        slot_acquired = False
        persistence_error: BaseException | None = None

        async with runtime.lock:
            if not task_brief.strip():
                raise SpawnRejectedError("blank_task_brief")
            if fork_mode != "none":
                raise SpawnRejectedError("unsupported_fork_mode")
            if trace_id is not None and not trace_id.strip():
                raise SpawnRejectedError("blank_trace_id")

            try:
                definition = get_role(role)
            except ValueError as exc:
                raise SpawnRejectedError("unknown_role") from exc
            selected_model = model or definition.model_key
            model_config = MODELS.get(selected_model)
            if model_config is None:
                raise SpawnRejectedError("unknown_model")
            if model_config.capability != "chat":
                raise SpawnRejectedError("model_not_chat")
            selected_tools = (
                definition.tools_allowlist
                if tools_allowlist is None
                else tuple(tools_allowlist)
            )
            if not set(selected_tools).issubset(definition.tools_allowlist):
                raise SpawnRejectedError("tool_allowlist_expansion")

            try:
                async with self._session_factory() as db:
                    existing_session = await db.get(AgentSession, session_id)
                    if existing_session is None:
                        raise SpawnRejectedError("session_not_found")

                    parent: AgentRegistry | None = None
                    depth = 1
                    if parent_agent_id is not None:
                        parent = await agent_registry_crud.get(db, parent_agent_id)
                        if parent is None:
                            raise SpawnRejectedError("parent_not_found")
                        if parent.session_id != session_id:
                            raise SpawnRejectedError("parent_session_mismatch")
                        ancestor_ids = {
                            part
                            for part in parent.agent_path.split("/")
                            if part and part != "root"
                        }
                        if ancestor_ids.intersection(runtime.closing_child_counts):
                            raise SpawnRejectedError("parent_closing")
                        if parent.status == "CLOSED":
                            raise SpawnRejectedError("parent_closed")
                        if role != "reviewer":
                            raise SpawnRejectedError("nested_role_not_allowed")
                        depth = self._depth_from_path(parent.agent_path) + 1
                    if depth > self._max_spawn_depth:
                        raise SpawnRejectedError(
                            "max_spawn_depth", limit_name="max_spawn_depth"
                        )

                    selected_budget = self._validated_budget(budget)
                    cumulative_count = await agent_registry_crud.count_for_session(
                        db, session_id
                    )
                    if cumulative_count >= self._max_children_per_session:
                        raise SpawnRejectedError(
                            "max_children_per_session",
                            limit_name="max_children_per_session",
                        )
                    active = await agent_registry_crud.list_active_children(
                        db, session_id
                    )
                    if (
                        len(active) >= self._max_concurrent_children
                        or len(runtime.held_child_ids)
                        >= self._max_concurrent_children
                        or runtime.slots.locked()
                    ):
                        raise SpawnRejectedError(
                            "max_concurrent_children",
                            limit_name="max_concurrent_children",
                        )
                    reserved_cost = (
                        await agent_registry_crud.reserved_cost_for_session(
                            db, session_id
                        )
                    )
                    if (
                        reserved_cost + selected_budget.max_cost_usd
                        > self._max_total_child_cost_usd
                    ):
                        raise SpawnRejectedError(
                            "max_total_child_cost_usd",
                            limit_name="max_total_child_cost_usd",
                        )

                    await runtime.slots.acquire()
                    slot_acquired = True
                    child_id = str(uuid4())
                    selected_trace_id = trace_id or str(uuid4())
                    agent_path = (
                        f"/root/{child_id}"
                        if parent is None
                        else f"{parent.agent_path}/{child_id}"
                    )
                    row = await agent_registry_crud.create(
                        db,
                        child_id=child_id,
                        session_id=session_id,
                        trace_id=selected_trace_id,
                        role_version=definition.version,
                        parent_agent_id=parent_agent_id,
                        agent_path=agent_path,
                        role=definition.name,
                        model=selected_model,
                        tools_allowlist=list(selected_tools),
                        sandbox_mode=definition.sandbox_mode,
                        task_brief=task_brief,
                        budget=_budget_payload(selected_budget),
                    )
                    await append_user_message(
                        db, session_id, task_brief, agent_id=child_id
                    )
                    await agent_trace_event_crud.record(
                        db,
                        trace_id=selected_trace_id,
                        session_id=session_id,
                        agent_id=child_id,
                        parent_agent_id=parent_agent_id,
                        step=0,
                        span_type="spawn",
                        control="REQUESTED",
                    )
                    row = await agent_registry_crud.transition_status(
                        db, child_id, "SPAWNING"
                    )
                    await db.commit()
                    receipt = _to_receipt(row)
            except BaseException as exc:
                if not slot_acquired or child_id is None:
                    if slot_acquired:
                        runtime.slots.release()
                        slot_acquired = False
                    raise
                persistence_error = exc

            if child_id is not None and persistence_error is None:
                runtime.held_child_ids.add(child_id)
            if persistence_error is None and child_id is not None:
                coroutine = self._execute_child(child_id)
                try:
                    task = _create_child_task(
                        coroutine,
                        name=f"child-agent:{child_id}",
                    )
                except BaseException:
                    coroutine.close()
                    create_task_failed = True
                else:
                    self._tasks[child_id] = task
                    task.add_done_callback(partial(self._task_done, child_id))

        if child_id is None:  # pragma: no cover - guarded by persistence
            raise RuntimeError("spawn persistence did not produce a child id")
        if persistence_error is not None:
            durable = await _await_reconciliation(
                self._reconcile_spawn_persistence(session_id, child_id)
            )
            if isinstance(persistence_error, asyncio.CancelledError):
                raise persistence_error
            if durable:
                raise ChildRuntimeUnavailableError(
                    child_id, reason="post_commit_failure"
                ) from persistence_error
            raise persistence_error
        if receipt is None:  # pragma: no cover - valid persisted rows always convert
            await _await_reconciliation(self._compensate_committed_spawn(child_id))
            raise ChildRuntimeUnavailableError(
                child_id, reason="receipt_conversion_failed"
            )
        if create_task_failed:
            await _await_reconciliation(self._compensate_committed_spawn(child_id))
            raise ChildRuntimeUnavailableError(
                child_id, reason="task_creation_failed"
            )
        return receipt

    async def _reconcile_spawn_persistence(
        self,
        session_id: int,
        child_id: str,
    ) -> bool:
        async with self._session_factory() as db:
            durable = await agent_registry_crud.get(db, child_id)
        runtime = self._runtime(session_id)
        if durable is None:
            async with runtime.lock:
                runtime.slots.release()
            return False

        async with runtime.lock:
            runtime.held_child_ids.add(child_id)
        await self._compensate_committed_spawn(child_id)
        return True

    async def _compensate_committed_spawn(self, child_id: str) -> None:
        receipt = await self._get_receipt(child_id)
        await self._persist_terminal(
            child_id,
            status="FAILED",
            budget=receipt.budget,
            result_summary=None,
            artifacts=(),
            error_class="infra",
        )
        await self._persist_close(child_id, force_closed=False)

    def _task_done(self, child_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(child_id) is task:
            self._tasks.pop(child_id, None)
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def _mark_running(
        self, child_id: str
    ) -> tuple[ChildReceipt | None, Budget | None]:
        async with self._session_factory() as lookup:
            row = await agent_registry_crud.get(lookup, child_id)
            if row is None:
                return None, None
            session_id = row.session_id
        runtime = self._runtime(session_id)
        async with runtime.lock:
            async with self._session_factory() as db:
                row = await agent_registry_crud.get(db, child_id)
                if row is None or row.status == "CLOSED":
                    return None, None
                if row.status != "SPAWNING":
                    return _to_receipt(row), None
                row = await agent_registry_crud.transition_status(
                    db, child_id, "RUNNING"
                )
                await db.commit()
                receipt = _to_receipt(row)
        active_budget = Budget(
            max_steps=receipt.budget.max_steps,
            max_cost_usd=receipt.budget.max_cost_usd,
        )
        return receipt, active_budget

    async def _execute_child(self, child_id: str) -> None:
        receipt: ChildReceipt | None = None
        active_budget: Budget | None = None
        wall_timeout: asyncio.Timeout | None = None
        try:
            receipt, active_budget = await self._mark_running(child_id)
            if receipt is None or active_budget is None:
                return
            self._active_budgets[child_id] = active_budget
            runner = self._child_runner or self._default_child_runner
            async with self._session_factory() as runner_db:
                wall_timeout = asyncio.timeout(
                    receipt.budget.max_wall_time_seconds
                )
                async with wall_timeout:
                    result = await runner(runner_db, receipt, active_budget)
                if wall_timeout.expired():
                    raise TimeoutError("child wall-time budget expired")
                await runner_db.commit()
        except asyncio.CancelledError:
            await self._persist_latest_terminal(
                child_id,
                receipt=receipt,
                active_budget=active_budget,
                status="CANCELLED",
                error_class=None,
            )
            raise
        except TimeoutError:
            await self._persist_latest_terminal(
                child_id,
                receipt=receipt,
                active_budget=active_budget,
                status="FAILED",
                error_class=(
                    "policy_reject"
                    if wall_timeout is not None and wall_timeout.expired()
                    else "infra"
                ),
            )
        except _ChildToolRuntimeError:
            await self._persist_latest_terminal(
                child_id,
                receipt=receipt,
                active_budget=active_budget,
                status="FAILED",
                error_class="tool",
            )
        except LlmRequestError:
            await self._persist_latest_terminal(
                child_id,
                receipt=receipt,
                active_budget=active_budget,
                status="FAILED",
                error_class="model",
            )
        except Exception:
            await self._persist_latest_terminal(
                child_id,
                receipt=receipt,
                active_budget=active_budget,
                status="FAILED",
                error_class="infra",
            )
        else:
            error_class = result.error_class
            if result.status == "FAILED" and error_class is None:
                error_class = "infra"
            if error_class is not None and error_class not in _SAFE_ERROR_CLASSES:
                error_class = "infra"
            try:
                await self._persist_terminal(
                    child_id,
                    status=result.status,
                    budget=self._snapshot_usage(receipt.budget, active_budget),
                    result_summary=result.result_summary,
                    artifacts=result.artifacts,
                    error_class=error_class,
                )
            except asyncio.CancelledError:
                await self._persist_latest_terminal(
                    child_id,
                    receipt=receipt,
                    active_budget=active_budget,
                    status="CANCELLED",
                    error_class=None,
                )
                raise
            except Exception:
                await self._persist_latest_terminal(
                    child_id,
                    receipt=receipt,
                    active_budget=active_budget,
                    status="FAILED",
                    error_class="infra",
                )
        finally:
            self._active_budgets.pop(child_id, None)

    @staticmethod
    def _snapshot_usage(
        configured: ChildBudgetSnapshot,
        active: Budget | None,
    ) -> ChildBudgetSnapshot:
        return ChildBudgetSnapshot(
            max_steps=configured.max_steps,
            max_cost_usd=configured.max_cost_usd,
            max_wall_time_seconds=configured.max_wall_time_seconds,
            steps_used=active.steps_used if active is not None else configured.steps_used,
            cost_used_usd=(
                active.cost_used_usd if active is not None else configured.cost_used_usd
            ),
        )

    async def _persist_latest_terminal(
        self,
        child_id: str,
        *,
        receipt: ChildReceipt | None,
        active_budget: Budget | None,
        status: Literal["FAILED", "CANCELLED"],
        error_class: ChildErrorClass | None,
    ) -> None:
        latest = receipt
        if latest is None:
            try:
                latest = await self._get_receipt(child_id)
            except ChildNotFoundError:
                return
        await self._persist_terminal(
            child_id,
            status=status,
            budget=self._snapshot_usage(latest.budget, active_budget),
            result_summary=None,
            artifacts=(),
            error_class=error_class,
        )

    async def _default_child_runner(
        self,
        db: AsyncSession,
        receipt: ChildReceipt,
        budget: Budget,
    ) -> ChildRunResult:
        definition = get_role(receipt.role)
        dispatcher = build_tool_dispatcher(db, receipt.tools_allowlist)

        async def dispatch_tool(
            name: str, arguments: dict[str, Any]
        ) -> ToolResult:
            try:
                return await dispatcher(name, arguments)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _ChildToolRuntimeError from exc

        outcome = await run_loop(
            db,
            session_id=receipt.session_id,
            model_key=receipt.model,
            dispatch_tool=dispatch_tool,
            tools=tool_schemas_for(receipt.tools_allowlist),
            budget=budget,
            chat_fn=self._chat_fn,
            agent_id=receipt.child_id,
            system_prompt=definition.instructions,
        )
        if outcome.reason == "final_answer":
            return ChildRunResult(
                status="COMPLETED", result_summary=outcome.final_answer
            )
        return ChildRunResult(
            status="FAILED",
            result_summary=None,
            error_class="policy_reject",
        )

    async def _persist_terminal(
        self,
        child_id: str,
        *,
        status: Literal["COMPLETED", "FAILED", "CANCELLED"],
        budget: ChildBudgetSnapshot,
        result_summary: str | None,
        artifacts: tuple[str, ...],
        error_class: ChildErrorClass | None,
    ) -> ChildReceipt | None:
        async with self._session_factory() as lookup:
            existing = await agent_registry_crud.get(lookup, child_id)
            if existing is None:
                return None
            session_id = existing.session_id
        runtime = self._runtime(session_id)
        async with runtime.lock:
            async with self._session_factory() as db:
                row = await agent_registry_crud.get(db, child_id)
                if row is None:
                    return None
                if row.status == "CLOSED" or row.status in _TERMINAL_STATUSES:
                    return _to_receipt(row)
                row = await agent_registry_crud.transition_status(
                    db,
                    child_id,
                    status,
                    budget=_budget_payload(budget),
                    result_summary=result_summary,
                    artifacts=list(artifacts),
                )
                await agent_trace_event_crud.record(
                    db,
                    trace_id=row.trace_id,
                    session_id=row.session_id,
                    agent_id=row.child_id,
                    parent_agent_id=row.parent_agent_id,
                    step=max(1, budget.steps_used),
                    span_type="agent",
                    control=status,
                    cost_usd=budget.cost_used_usd,
                    error_class=error_class,
                )
                await db.commit()
                return _to_receipt(row)

    async def _get_receipt(self, child_id: str) -> ChildReceipt:
        async with self._session_factory() as db:
            row = await agent_registry_crud.get(db, child_id)
            if row is None:
                raise ChildNotFoundError(child_id)
            return _to_receipt(row)

    async def wait_agent(
        self,
        child_id: str,
        *,
        timeout_ms: int | None = None,
    ) -> ChildReceipt:
        """Wait for a local task without allowing the wait deadline to cancel it."""
        receipt = await self._get_receipt(child_id)
        if receipt.status in _TERMINAL_STATUSES:
            return receipt
        task = self._tasks.get(child_id)
        if task is None:
            latest = await self._get_receipt(child_id)
            if latest.status in _TERMINAL_STATUSES:
                return latest
            raise ChildRuntimeUnavailableError(child_id)
        try:
            shielded = asyncio.shield(task)
            if timeout_ms is None:
                await shielded
            else:
                await asyncio.wait_for(shielded, timeout_ms / 1000)
        except TimeoutError as exc:
            assert timeout_ms is not None
            raise ChildWaitTimeoutError(child_id, timeout_ms) from exc
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        return await self._get_receipt(child_id)

    async def send_input(self, child_id: str, message: str) -> ChildReceipt:
        """Append one user message only while the exact child is RUNNING."""
        if not message.strip():
            raise SpawnRejectedError("blank_input")
        receipt = await self._get_receipt(child_id)
        runtime = self._runtime(receipt.session_id)
        async with runtime.lock:
            async with self._session_factory() as db:
                row = await agent_registry_crud.get(db, child_id)
                if row is None:
                    raise ChildNotFoundError(child_id)
                if row.status != "RUNNING":
                    raise SpawnRejectedError("child_not_running")
                await append_user_message(
                    db, row.session_id, message, agent_id=child_id
                )
                await db.commit()
                return _to_receipt(row)

    async def close_agent(self, child_id: str) -> ChildReceipt:
        """Close descendants deepest-first, then idempotently close the target."""
        target = await self._get_receipt(child_id)
        runtime = self._runtime(target.session_id)
        async with runtime.lock:
            async with self._session_factory() as db:
                descendants = await agent_registry_crud.list_descendants(
                    db,
                    target.session_id,
                    child_id,
                    deepest_first=True,
                )
                descendant_ids = [row.child_id for row in descendants]
            closing_ids = [*descendant_ids, child_id]
            for closing_id in closing_ids:
                runtime.closing_child_counts[closing_id] = (
                    runtime.closing_child_counts.get(closing_id, 0) + 1
                )
        try:
            for descendant_id in descendant_ids:
                await self._close_one(descendant_id)
            return await self._close_one(child_id)
        finally:
            cleanup_task = asyncio.create_task(
                self._release_closing_admission(runtime, closing_ids)
            )
            cancellation: asyncio.CancelledError | None = None
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError as exc:
                    cancellation = exc
            cleanup_task.result()
            if cancellation is not None:
                raise cancellation

    @staticmethod
    async def _release_closing_admission(
        runtime: _SessionRuntime,
        closing_ids: list[str],
    ) -> None:
        async with runtime.lock:
            for closing_id in closing_ids:
                remaining = runtime.closing_child_counts[closing_id] - 1
                if remaining == 0:
                    runtime.closing_child_counts.pop(closing_id)
                else:
                    runtime.closing_child_counts[closing_id] = remaining

    async def _close_one(self, child_id: str) -> ChildReceipt:
        receipt = await self._get_receipt(child_id)
        if receipt.status == "CLOSED":
            await self._release_owned_slot(receipt.session_id, child_id)
            return receipt

        task = self._tasks.get(child_id)
        force_closed = False
        if task is not None and not task.done():
            task.cancel()
            done, _pending = await asyncio.wait(
                {task}, timeout=self._close_timeout_seconds
            )
            force_closed = not done

        latest = await self._get_receipt(child_id)
        if latest.status in _ACTIVE_STATUSES:
            live_budget = self._active_budgets.get(child_id)
            persisted = await self._persist_terminal(
                child_id,
                status="CANCELLED",
                budget=self._snapshot_usage(latest.budget, live_budget),
                result_summary=None,
                artifacts=latest.artifacts,
                error_class=None,
            )
            if persisted is not None:
                latest = persisted
        if latest.status == "CLOSED":
            await self._release_owned_slot(latest.session_id, child_id)
            return latest
        closed = await self._persist_close(
            child_id,
            force_closed=force_closed,
            force_detached_task=task if force_closed else None,
        )
        if force_closed:
            self._release_force_detached_ownership(child_id, task)
        return closed

    async def _persist_close(
        self,
        child_id: str,
        *,
        force_closed: bool,
        force_detached_task: asyncio.Task[None] | None = None,
    ) -> ChildReceipt:
        receipt = await self._get_receipt(child_id)
        runtime = self._runtime(receipt.session_id)
        closed: ChildReceipt | None = None
        persistence_error: BaseException | None = None
        async with runtime.lock:
            try:
                async with self._session_factory() as db:
                    row = await agent_registry_crud.get(db, child_id)
                    if row is None:
                        raise ChildNotFoundError(child_id)
                    changed = row.status != "CLOSED"
                    if changed:
                        row = await agent_registry_crud.close(
                            db, child_id, force_closed=force_closed
                        )
                        budget = _to_receipt(row).budget
                        await agent_trace_event_crud.record(
                            db,
                            trace_id=row.trace_id,
                            session_id=row.session_id,
                            agent_id=row.child_id,
                            parent_agent_id=row.parent_agent_id,
                            step=max(2, budget.steps_used + 1),
                            span_type="close",
                            control="CLOSED",
                            cost_usd=budget.cost_used_usd,
                            error_class="infra" if force_closed else None,
                        )
                        await db.commit()
                    closed = _to_receipt(row)
            except BaseException as exc:
                persistence_error = exc
            else:
                if child_id in runtime.held_child_ids:
                    runtime.held_child_ids.remove(child_id)
                    runtime.slots.release()

        if persistence_error is not None:
            await _await_reconciliation(
                self._reconcile_close_persistence(
                    receipt.session_id,
                    child_id,
                    force_detached_task,
                )
            )
            raise persistence_error
        if closed is None:  # pragma: no cover - valid rows always convert
            raise ChildRuntimeUnavailableError(
                child_id,
                reason="close_receipt_unavailable",
            )
        return closed

    async def _reconcile_close_persistence(
        self,
        session_id: int,
        child_id: str,
        force_detached_task: asyncio.Task[None] | None,
    ) -> None:
        async with self._session_factory() as db:
            row = await agent_registry_crud.get(db, child_id)
            durable_closed = row is not None and row.status == "CLOSED"
            durable_force_closed = (
                row is not None
                and row.status == "CLOSED"
                and row.force_closed is True
            )
        if durable_closed:
            await self._release_owned_slot(session_id, child_id)
        if durable_force_closed:
            self._release_force_detached_ownership(child_id, force_detached_task)

    def _release_force_detached_ownership(
        self,
        child_id: str,
        task: asyncio.Task[None] | None,
    ) -> None:
        if task is not None and self._tasks.get(child_id) is task:
            self._tasks.pop(child_id)
            self._active_budgets.pop(child_id, None)

    async def _release_owned_slot(self, session_id: int, child_id: str) -> None:
        runtime = self._runtime(session_id)
        async with runtime.lock:
            if child_id in runtime.held_child_ids:
                runtime.held_child_ids.remove(child_id)
                runtime.slots.release()

    async def list_agents(self, session_id: int) -> tuple[ChildReceipt, ...]:
        """Return every persisted receipt for one session in stable CRUD order."""
        async with self._session_factory() as db:
            rows = await agent_registry_crud.list_for_session(db, session_id)
            return tuple(_to_receipt(row) for row in rows)


spawn_manager = SpawnManager(AsyncSessionLocal)
