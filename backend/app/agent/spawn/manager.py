"""SpawnManager：子 Agent 的进程内生命周期编排。

负责 spawn/wait/send/close/list 五个动作、并发槽与 session 锁、任务与预算的进程内状态，
以及崩溃恢复与回执 GC。数据契约见 types，回执转换见 receipts，准入校验见 admission。

注意：测试通过 monkeypatch 打桩本模块的 _create_child_task 与 build_tool_dispatcher，
两者必须在本模块命名空间里被查找，不要改成从别处间接引用。
"""

import asyncio
import logging
from collections.abc import Coroutine, Iterable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.budget import Budget
from app.agent.loop import ChatFn, ToolResult, run_loop
from app.agent.roles import get_role
from app.agent.session import append_user_message
from app.agent.spawn.admission import depth_from_path, path_depth, validate_child_budget
from app.agent.spawn.receipts import _budget_payload, _to_receipt
from app.agent.spawn.types import (
    _ACTIVE_STATUSES,
    _SAFE_ERROR_CLASSES,
    _TERMINAL_STATUSES,
    ChildBudgetSnapshot,
    ChildErrorClass,
    ChildNotFoundError,
    ChildReceipt,
    ChildRunner,
    ChildRunResult,
    ChildRuntimeUnavailableError,
    ChildWaitTimeoutError,
    NoopSpawnEventPublisher,
    SpawnEventPublisher,
    SpawnRejectedError,
    _ChildToolRuntimeError,
    _SessionRuntime,
)
from app.agent.tool_dispatch import build_tool_dispatcher, tool_schemas_for
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.llm import MODELS, LlmRequestError, chat
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.models.agent_registry import AgentRegistry
from app.models.agent_session import AgentSession

logger = logging.getLogger(__name__)


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
        terminal_receipt_ttl_seconds: float = settings.AGENT_TERMINAL_RECEIPT_TTL_SECONDS,
        receipt_gc_interval_seconds: float = settings.AGENT_RECEIPT_GC_INTERVAL_SECONDS,
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
        self.terminal_receipt_ttl_seconds = terminal_receipt_ttl_seconds
        self.receipt_gc_interval_seconds = receipt_gc_interval_seconds
        self._session_runtimes: dict[int, _SessionRuntime] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._active_budgets: dict[str, Budget] = {}
        self._event_publisher: SpawnEventPublisher = NoopSpawnEventPublisher()

    def set_event_publisher(self, publisher: SpawnEventPublisher) -> None:
        """注入子 Agent WS 事件发布器（lifespan 启动时绑定 hub）。"""
        self._event_publisher = publisher

    async def _publish_child_status(self, receipt: ChildReceipt) -> None:
        try:
            await self._event_publisher.publish_child_status(receipt)
        except Exception:
            logger.exception(
                "发布子 Agent 状态失败",
                extra={"child_id": receipt.child_id},
            )

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
                        depth = depth_from_path(parent.agent_path) + 1
                    if depth > self._max_spawn_depth:
                        raise SpawnRejectedError(
                            "max_spawn_depth", limit_name="max_spawn_depth"
                        )

                    selected_budget = validate_child_budget(
                        budget,
                        max_steps=self._child_max_steps,
                        max_cost_usd=self._child_max_cost_usd,
                        max_wall_time_seconds=self._child_max_wall_time_seconds,
                    )
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
                    await self._publish_child_status(receipt)
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
        await self._publish_child_status(receipt)
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
                    "budget_exceeded"
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
        match outcome.reason:
            case "budget_exceeded":
                error_class: ChildErrorClass = "budget_exceeded"
            case "llm_error":
                error_class = "model"
            case "early_exit":
                error_class = "policy_reject"
        return ChildRunResult(
            status="FAILED",
            result_summary=None,
            error_class=error_class,
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
        published_receipt: ChildReceipt | None = None
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
                published_receipt = _to_receipt(row)
        if published_receipt is not None:
            await self._publish_child_status(published_receipt)
        return published_receipt

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
        error_class: ChildErrorClass | None = None,
    ) -> ChildReceipt:
        receipt = await self._get_receipt(child_id)
        runtime = self._runtime(receipt.session_id)
        closed: ChildReceipt | None = None
        persistence_error: BaseException | None = None
        publish_closed = False
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
                            error_class="infra" if force_closed else error_class,
                        )
                        await db.commit()
                        publish_closed = True
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
        if publish_closed:
            await self._publish_child_status(closed)
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

    async def _recover_close_one(
        self,
        child_id: str,
        *,
        error_class: ChildErrorClass | None,
    ) -> ChildReceipt:
        """Close one durable row without assuming this process ran its task.

        Shared by `reconcile_startup` (every row is an orphan this fresh
        manager never held) and `collect_expired_receipts` (a row may or may
        not be locally owned). An active row is legal-transitioned to
        CANCELLED before CLOSED; a terminal row goes straight to CLOSED.
        Never touches `self._tasks`/`self._active_budgets`, and only releases
        a concurrency slot when `child_id` is already in this manager's
        `held_child_ids` — empty for every orphan, populated only for a row
        this same process actually ran and never closed.
        """
        latest = await self._get_receipt(child_id)
        if latest.status == "CLOSED":
            await self._release_owned_slot(latest.session_id, child_id)
            return latest
        if latest.status in _ACTIVE_STATUSES:
            persisted = await self._persist_terminal(
                child_id,
                status="CANCELLED",
                budget=latest.budget,
                result_summary=None,
                artifacts=latest.artifacts,
                error_class=error_class,
            )
            if persisted is not None:
                latest = persisted
        if latest.status == "CLOSED":
            await self._release_owned_slot(latest.session_id, child_id)
            return latest
        return await self._persist_close(
            child_id, force_closed=False, error_class=error_class
        )

    async def reconcile_startup(self) -> tuple[ChildReceipt, ...]:
        """Close every non-CLOSED row at process boot; this fresh manager owns no tasks.

        Every row returned by the cross-session query is by definition an
        orphan: a brand-new `SpawnManager` has an empty `_tasks` map and an
        empty `held_child_ids` set for every session, so no row here can be
        one this process is actually running. Descendants close before their
        parents. Every recovery close is tagged `error_class="infra"` because
        the process genuinely lost runtime ownership of the row.
        """
        async with self._session_factory() as db:
            rows = await agent_registry_crud.list_active(db)
        ordered = sorted(
            rows,
            key=lambda row: (-path_depth(row.agent_path), row.created_at, row.child_id),
        )
        closed = [
            await self._recover_close_one(row.child_id, error_class="infra")
            for row in ordered
        ]
        return tuple(closed)

    async def collect_expired_receipts(
        self, now: datetime | None = None
    ) -> tuple[ChildReceipt, ...]:
        """Close terminal receipts whose lifecycle clock is older than the TTL.

        Registry rows and message transcripts are never deleted — this is
        lifecycle cleanup (freeing held concurrency slots and marking rows
        CLOSED), not physical deletion. Unlike `reconcile_startup`, a
        collected row may be one this same process ran to completion, so the
        close trace is not tagged `error_class="infra"`.
        """
        reference = now if now is not None else datetime.now(UTC)
        cutoff = reference - timedelta(seconds=self.terminal_receipt_ttl_seconds)
        async with self._session_factory() as db:
            rows = await agent_registry_crud.list_terminal_before(db, cutoff)
        closed = [
            await self._recover_close_one(row.child_id, error_class=None) for row in rows
        ]
        return tuple(closed)

    async def shutdown(self) -> None:
        """Cancel and close every child this process still owns; safe to call twice.

        Closing a locally held root cascades through `close_agent` to its
        descendants, so one pass over the roots handles most local children;
        a second pass over whatever remains catches any id a root's cascade
        didn't reach. Once every local task and held slot is gone, a repeat
        call finds nothing to do and returns immediately.
        """
        local_ids = set(self._tasks)
        for runtime in self._session_runtimes.values():
            local_ids |= runtime.held_child_ids
        if not local_ids:
            return

        receipts = {child_id: await self._get_receipt(child_id) for child_id in local_ids}
        roots = [
            child_id
            for child_id in local_ids
            if receipts[child_id].parent_agent_id is None
            or receipts[child_id].parent_agent_id not in local_ids
        ]
        for child_id in roots:
            await self.close_agent(child_id)
        for child_id in local_ids - set(roots):
            await self.close_agent(child_id)

        assert not self._tasks
        assert all(not runtime.held_child_ids for runtime in self._session_runtimes.values())


async def run_receipt_gc_loop(manager: SpawnManager) -> None:
    """Run `collect_expired_receipts` forever, sleeping `receipt_gc_interval_seconds` between rounds."""
    while True:
        try:
            await manager.collect_expired_receipts()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("子 Agent 回执 GC 失败")
        await asyncio.sleep(manager.receipt_gc_interval_seconds)


spawn_manager = SpawnManager(AsyncSessionLocal)
