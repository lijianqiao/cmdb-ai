"""SpawnManager 的公开回执与创建前校验测试。

实现流程：
1. 使用临时 SQLite 文件和真实 registry CRUD，模拟一个可持久化的用户会话。
2. 先验证 child 回执是不可变快照，调用方不能改写 ORM 生命周期事实。
3. 再逐项提交非法 Spawn 请求，并确认校验发生在 receipt、消息和 trace 创建前。
4. 对数量与费用使用真实完成/关闭流程，证明配额依据持久化历史而非本地计数猜测。
"""

from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime
from math import inf, nan
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agent.budget import Budget
from app.agent.loop import ToolResult
from app.agent.spawn import (
    ChildBudgetSnapshot,
    ChildNotFoundError,
    ChildReceipt,
    ChildReceiptCorruptionError,
    ChildRunResult,
    ChildRuntimeUnavailableError,
    ChildWaitTimeoutError,
    SpawnManager,
    SpawnRejectedError,
)
from app.core.llm import ChatMessage, ChatResult, LlmRequestError, ToolCall
from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.models import Base
from app.models.agent_session import AgentSession
from app.models.agent_trace_event import AgentTraceEvent
from app.models.user import User


@dataclass(slots=True)
class SpawnDatabase:
    session_factory: async_sessionmaker[AsyncSession]
    session_id: int
    other_session_id: int


async def _completed_runner(
    _db: AsyncSession,
    _receipt: ChildReceipt,
    _budget: Budget,
) -> ChildRunResult:
    return ChildRunResult(status="COMPLETED", result_summary="done")


@pytest_asyncio.fixture
async def spawn_db(tmp_path: Path) -> AsyncIterator[SpawnDatabase]:
    """Create independent DB connections so child sessions can run concurrently."""
    database_path = tmp_path / "spawn.sqlite3"
    engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as db:
        user = User(
            username="spawn-user",
            email="spawn@example.com",
            hashed_password="not-used",
            nickname="Spawn",
        )
        db.add(user)
        await db.flush()
        first = AgentSession(user_id=user.id, title="first", status="active")
        second = AgentSession(user_id=user.id, title="second", status="active")
        db.add_all([first, second])
        await db.commit()
        session_ids = (first.id, second.id)

    try:
        yield SpawnDatabase(
            session_factory=session_factory,
            session_id=session_ids[0],
            other_session_id=session_ids[1],
        )
    finally:
        # 强制 detach 路径（close_agent 等待超时后放弃）会留下没跑完 __aexit__ 的
        # 子 session，它持有的连接不在池里，engine.dispose() 收不到，只能等 GC。
        # 不在这里显式回收的话，终结动作会落在后续任意一个测试的 teardown 里，
        # 表现为跨文件的、随代码布局漂移的 StaticPool CancelledError。
        # 在本 fixture 作用域内收干净，把这个既有泄漏关在它自己的测试里。
        gc.collect()
        await engine.dispose()


def _make_receipt(*, status: str = "REQUESTED") -> ChildReceipt:
    now = datetime.now(UTC)
    return ChildReceipt(
        child_id="child-1",
        trace_id="trace-1",
        session_id=1,
        parent_agent_id=None,
        agent_path="/root/child-1",
        role="kb_explorer",
        role_version="t09-v1",
        model="local-chat",
        tools_allowlist=("kb_read",),
        sandbox_mode="read-only",
        task_brief="读取指定 SOP",
        budget=ChildBudgetSnapshot(
            max_steps=5,
            max_cost_usd=0.5,
            max_wall_time_seconds=30.0,
        ),
        status=status,
        result_summary=None,
        artifacts=(),
        created_at=now,
        status_changed_at=now,
        closed_at=None,
        force_closed=False,
    )


def test_child_contracts_are_immutable() -> None:
    receipt = _make_receipt()
    result = ChildRunResult(status="COMPLETED", result_summary="done")

    with pytest.raises(FrozenInstanceError):
        receipt.status = "RUNNING"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        receipt.budget.steps_used = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.status = "FAILED"  # type: ignore[misc]


async def _assert_no_receipts(manager: SpawnManager, session_id: int) -> None:
    assert await manager.list_agents(session_id) == ()


async def _persist_raw_receipt(
    spawn_db: SpawnDatabase,
    *,
    budget: dict[str, object],
    tools_allowlist: list[object] | None = None,
    artifacts: list[object] | None = None,
) -> str:
    child_id = "corrupt-child"
    async with spawn_db.session_factory() as db:
        row = await agent_registry_crud.create(
            db,
            child_id=child_id,
            session_id=spawn_db.session_id,
            trace_id="corrupt-trace",
            role_version="t09-v1",
            parent_agent_id=None,
            agent_path=f"/root/{child_id}",
            role="kb_explorer",
            model="local-chat",
            tools_allowlist=(
                ["kb_read"] if tools_allowlist is None else tools_allowlist
            ),  # type: ignore[arg-type]
            sandbox_mode="read-only",
            task_brief="corrupt durable payload",
            budget=budget,
        )
        if artifacts is not None:
            row.artifacts = artifacts  # type: ignore[assignment]
        await db.commit()
    return child_id


async def _persist_raw_completed_receipt(
    spawn_db: SpawnDatabase,
    *,
    budget: dict[str, object],
) -> str:
    child_id = await _persist_raw_receipt(spawn_db, budget=budget)
    async with spawn_db.session_factory() as db:
        await agent_registry_crud.transition_status(db, child_id, "SPAWNING")
        await agent_registry_crud.transition_status(db, child_id, "RUNNING")
        await agent_registry_crud.transition_status(db, child_id, "COMPLETED")
        await db.commit()
    return child_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_steps", 2.9),
        ("max_steps", True),
        ("max_steps", 0),
        ("steps_used", 1.9),
        ("steps_used", -1),
        ("steps_used", 6),
        ("max_cost_usd", nan),
        ("max_cost_usd", inf),
        ("max_cost_usd", -0.1),
        ("max_cost_usd", "do-not-leak"),
        ("max_wall_time_seconds", nan),
        ("max_wall_time_seconds", inf),
        ("max_wall_time_seconds", 0.0),
        ("max_wall_time_seconds", -1.0),
        ("cost_used_usd", nan),
        ("cost_used_usd", inf),
        ("cost_used_usd", -0.1),
    ],
)
async def test_list_agents_rejects_corrupt_budget_snapshot(
    spawn_db: SpawnDatabase,
    field: str,
    value: object,
) -> None:
    budget: dict[str, object] = {
        "max_steps": 5,
        "max_cost_usd": 0.5,
        "max_wall_time_seconds": 30.0,
        "steps_used": 0,
        "cost_used_usd": 0.0,
    }
    budget[field] = value
    child_id = await _persist_raw_receipt(spawn_db, budget=budget)
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(ChildReceiptCorruptionError) as raised:
        await manager.list_agents(spawn_db.session_id)

    assert raised.value.child_id == child_id
    assert raised.value.field == f"budget.{field}"
    assert "do-not-leak" not in str(raised.value)


async def test_list_agents_wraps_overflowing_durable_number_as_corruption(
    spawn_db: SpawnDatabase,
) -> None:
    oversized_value = 10**400
    child_id = await _persist_raw_receipt(
        spawn_db,
        budget={
            "max_steps": 5,
            "max_cost_usd": oversized_value,
            "max_wall_time_seconds": 30.0,
            "steps_used": 0,
            "cost_used_usd": 0.0,
        },
    )
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(ChildReceiptCorruptionError) as raised:
        await manager.list_agents(spawn_db.session_id)

    assert raised.value.child_id == child_id
    assert raised.value.field == "budget.max_cost_usd"
    assert str(oversized_value) not in str(raised.value)


@pytest.mark.parametrize(
    ("field", "max_steps", "steps_used"),
    [
        ("budget.max_steps", 10**400, 0),
        ("budget.steps_used", 10**400, 10**400),
    ],
)
async def test_list_agents_rejects_durable_step_outside_trace_range(
    spawn_db: SpawnDatabase,
    field: str,
    max_steps: int,
    steps_used: int,
) -> None:
    child_id = await _persist_raw_receipt(
        spawn_db,
        budget={
            "max_steps": max_steps,
            "max_cost_usd": 0.5,
            "max_wall_time_seconds": 30.0,
            "steps_used": steps_used,
            "cost_used_usd": 0.0,
        },
    )
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(ChildReceiptCorruptionError) as raised:
        await manager.list_agents(spawn_db.session_id)

    assert raised.value.child_id == child_id
    assert raised.value.field == field
    assert str(max_steps) not in str(raised.value)


async def test_close_rejects_durable_step_above_trace_range_before_trace(
    spawn_db: SpawnDatabase,
) -> None:
    first_unsafe_step = 2_147_483_647
    child_id = await _persist_raw_completed_receipt(
        spawn_db,
        budget={
            "max_steps": first_unsafe_step,
            "max_cost_usd": 0.5,
            "max_wall_time_seconds": 30.0,
            "steps_used": first_unsafe_step,
            "cost_used_usd": 0.0,
        },
    )
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)
    corruption: ChildReceiptCorruptionError | None = None

    try:
        await manager.close_agent(child_id)
    except ChildReceiptCorruptionError as exc:
        corruption = exc

    async with spawn_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, "corrupt-trace")
    assert events == []
    assert corruption is not None
    assert corruption.child_id == child_id
    assert corruption.field == "budget.steps_used"
    assert str(first_unsafe_step) not in str(corruption)


async def test_close_accepts_largest_trace_safe_durable_step(
    spawn_db: SpawnDatabase,
) -> None:
    largest_safe_step = 2_147_483_646
    child_id = await _persist_raw_completed_receipt(
        spawn_db,
        budget={
            "max_steps": largest_safe_step,
            "max_cost_usd": 0.5,
            "max_wall_time_seconds": 30.0,
            "steps_used": largest_safe_step,
            "cost_used_usd": 0.0,
        },
    )
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    closed = await manager.close_agent(child_id)

    async with spawn_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, "corrupt-trace")
    assert closed.status == "CLOSED"
    assert [(event.control, event.step) for event in events] == [
        ("CLOSED", 2_147_483_647)
    ]


@pytest.mark.parametrize(
    ("field", "tools_allowlist", "artifacts"),
    [
        ("tools_allowlist", ["kb_read", 7], None),
        ("tools_allowlist", ["kb_read", {"secret": "do-not-leak"}], None),
        ("artifacts", None, [7]),
        ("artifacts", None, [{"secret": "do-not-leak"}]),
    ],
)
async def test_list_agents_rejects_non_string_receipt_collections(
    spawn_db: SpawnDatabase,
    field: str,
    tools_allowlist: list[object] | None,
    artifacts: list[object] | None,
) -> None:
    child_id = await _persist_raw_receipt(
        spawn_db,
        budget={
            "max_steps": 5,
            "max_cost_usd": 0.5,
            "max_wall_time_seconds": 30.0,
            "steps_used": 0,
            "cost_used_usd": 0.0,
        },
        tools_allowlist=tools_allowlist,
        artifacts=artifacts,
    )
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(ChildReceiptCorruptionError) as raised:
        await manager.list_agents(spawn_db.session_id)

    assert raised.value.child_id == child_id
    assert raised.value.field == field
    assert "do-not-leak" not in str(raised.value)


@pytest.mark.parametrize("brief", ["", " ", "\n\t"])
async def test_spawn_rejects_blank_brief_before_persisting(
    spawn_db: SpawnDatabase,
    brief: str,
) -> None:
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief=brief,
        )

    assert raised.value.reason == "blank_task_brief"
    await _assert_no_receipts(manager, spawn_db.session_id)


async def test_spawn_rejects_blank_trace_id_before_persisting(
    spawn_db: SpawnDatabase,
) -> None:
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="读取 SOP",
            trace_id=" ",
        )

    assert raised.value.reason == "blank_trace_id"
    await _assert_no_receipts(manager, spawn_db.session_id)


@pytest.mark.parametrize("fork_mode", ["all", "parent", "recent"])
async def test_spawn_rejects_fork_mode_other_than_none(
    spawn_db: SpawnDatabase,
    fork_mode: str,
) -> None:
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="读取 SOP",
            fork_mode=fork_mode,
        )

    assert raised.value.reason == "unsupported_fork_mode"
    await _assert_no_receipts(manager, spawn_db.session_id)


@pytest.mark.parametrize(
    ("role", "model", "reason"),
    [
        ("missing-role", None, "unknown_role"),
        ("kb_explorer", "missing-model", "unknown_model"),
        ("kb_explorer", "local-embedding", "model_not_chat"),
    ],
)
async def test_spawn_rejects_unknown_role_and_non_chat_model(
    spawn_db: SpawnDatabase,
    role: str,
    model: str | None,
    reason: str,
) -> None:
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role=role,
            task_brief="读取 SOP",
            model=model,
        )

    assert raised.value.reason == reason
    await _assert_no_receipts(manager, spawn_db.session_id)


async def test_spawn_rejects_tool_allowlist_expansion(
    spawn_db: SpawnDatabase,
) -> None:
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="classifier",
            task_brief="归类两份文档",
            tools_allowlist=("kb_read", "query_cmdb"),
        )

    assert raised.value.reason == "tool_allowlist_expansion"
    await _assert_no_receipts(manager, spawn_db.session_id)


async def test_spawn_rejects_missing_session_before_persisting(
    spawn_db: SpawnDatabase,
) -> None:
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=999_999,
            role="kb_explorer",
            task_brief="读取 SOP",
        )

    assert raised.value.reason == "session_not_found"
    await _assert_no_receipts(manager, 999_999)


async def test_spawn_rejects_parent_from_another_session(
    spawn_db: SpawnDatabase,
) -> None:
    gate = asyncio.Event()

    async def gated_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        await gate.wait()
        return ChildRunResult(status="COMPLETED", result_summary="done")

    manager = SpawnManager(spawn_db.session_factory, child_runner=gated_runner)
    parent = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="investigator",
        task_brief="调查一个假设",
    )

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=spawn_db.other_session_id,
            role="reviewer",
            task_brief="复核调查证据",
            parent_agent_id=parent.child_id,
        )

    assert raised.value.reason == "parent_session_mismatch"
    await _assert_no_receipts(manager, spawn_db.other_session_id)
    gate.set()
    await manager.wait_agent(parent.child_id)
    await manager.close_agent(parent.child_id)


async def test_spawn_rejects_missing_and_closed_parent(
    spawn_db: SpawnDatabase,
) -> None:
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(SpawnRejectedError) as missing:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="reviewer",
            task_brief="复核",
            parent_agent_id="missing-child",
        )
    assert missing.value.reason == "parent_not_found"

    parent = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="investigator",
        task_brief="调查",
    )
    await manager.wait_agent(parent.child_id)
    await manager.close_agent(parent.child_id)
    before = len(await manager.list_agents(spawn_db.session_id))

    with pytest.raises(SpawnRejectedError) as closed:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="reviewer",
            task_brief="复核",
            parent_agent_id=parent.child_id,
        )

    assert closed.value.reason == "parent_closed"
    assert len(await manager.list_agents(spawn_db.session_id)) == before


async def test_only_reviewer_can_be_nested_and_depth_three_is_rejected(
    spawn_db: SpawnDatabase,
) -> None:
    gates: dict[str, asyncio.Event] = {}

    async def gated_runner(
        _db: AsyncSession,
        receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        await gates.setdefault(receipt.child_id, asyncio.Event()).wait()
        return ChildRunResult(status="COMPLETED", result_summary="done")

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=gated_runner,
        max_spawn_depth=2,
    )
    parent = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="investigator",
        task_brief="调查",
    )
    reviewer = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="reviewer",
        task_brief="复核",
        parent_agent_id=parent.child_id,
    )
    before = len(await manager.list_agents(spawn_db.session_id))

    with pytest.raises(SpawnRejectedError) as wrong_role:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="classifier",
            task_brief="不允许嵌套",
            parent_agent_id=parent.child_id,
        )
    with pytest.raises(SpawnRejectedError) as too_deep:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="reviewer",
            task_brief="第三层",
            parent_agent_id=reviewer.child_id,
        )

    assert wrong_role.value.reason == "nested_role_not_allowed"
    assert too_deep.value.reason == "max_spawn_depth"
    assert too_deep.value.limit_name == "max_spawn_depth"
    assert len(await manager.list_agents(spawn_db.session_id)) == before
    for child_id in (reviewer.child_id, parent.child_id):
        gates.setdefault(child_id, asyncio.Event()).set()
    await manager.wait_agent(reviewer.child_id)
    await manager.wait_agent(parent.child_id)
    await manager.close_agent(parent.child_id)


async def test_session_child_count_is_cumulative_even_after_close(
    spawn_db: SpawnDatabase,
) -> None:
    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=_completed_runner,
        max_concurrent_children=1,
        max_children_per_session=2,
    )
    for index in range(2):
        receipt = await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief=f"读取 SOP {index}",
        )
        await manager.wait_agent(receipt.child_id)
        await manager.close_agent(receipt.child_id)

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="第三次读取",
        )

    assert raised.value.reason == "max_children_per_session"
    assert raised.value.limit_name == "max_children_per_session"
    assert len(await manager.list_agents(spawn_db.session_id)) == 2


async def test_session_child_budget_is_reserved_conservatively(
    spawn_db: SpawnDatabase,
) -> None:
    gate = asyncio.Event()

    async def reserved_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        budget: Budget,
    ) -> ChildRunResult:
        await gate.wait()
        budget.record_step(0.4)
        return ChildRunResult(status="COMPLETED", result_summary="done")

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=reserved_runner,
        max_concurrent_children=2,
        max_total_child_cost_usd=1.0,
    )
    active = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="保守预留",
        budget=ChildBudgetSnapshot(5, 0.6, 30.0),
    )

    with pytest.raises(SpawnRejectedError) as active_rejection:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="超过 active 预留",
            budget=ChildBudgetSnapshot(5, 0.5, 30.0),
        )
    assert active_rejection.value.reason == "max_total_child_cost_usd"
    assert len(await manager.list_agents(spawn_db.session_id)) == 1

    gate.set()
    await manager.wait_agent(active.child_id)
    await manager.close_agent(active.child_id)

    with pytest.raises(SpawnRejectedError) as actual_rejection:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="超过终态实际费用",
            budget=ChildBudgetSnapshot(5, 0.7, 30.0),
        )
    assert actual_rejection.value.reason == "max_total_child_cost_usd"
    assert len(await manager.list_agents(spawn_db.session_id)) == 1


@pytest.mark.parametrize(
    "budget",
    [
        ChildBudgetSnapshot(0, 0.5, 30.0),
        ChildBudgetSnapshot(21, 0.5, 30.0),
        ChildBudgetSnapshot(5, 1.01, 30.0),
        ChildBudgetSnapshot(5, 0.5, 120.01),
    ],
)
async def test_budget_override_must_only_tighten_configured_child_limits(
    spawn_db: SpawnDatabase,
    budget: ChildBudgetSnapshot,
) -> None:
    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=_completed_runner,
        child_max_steps=20,
        child_max_cost_usd=1.0,
        child_max_wall_time_seconds=120.0,
    )

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="非法预算",
            budget=budget,
        )

    assert raised.value.reason == "invalid_child_budget"
    assert raised.value.limit_name is not None
    await _assert_no_receipts(manager, spawn_db.session_id)


@pytest.mark.parametrize(
    "budget",
    [
        ChildBudgetSnapshot(5, -0.01, 30.0),
        ChildBudgetSnapshot(5, nan, 30.0),
        ChildBudgetSnapshot(5, inf, 30.0),
        ChildBudgetSnapshot(5, 0.5, 0.0),
        ChildBudgetSnapshot(5, 0.5, nan),
        ChildBudgetSnapshot(5, 0.5, inf),
        ChildBudgetSnapshot(5, 0.5, 30.0, steps_used=1),
        ChildBudgetSnapshot(5, 0.5, 30.0, cost_used_usd=0.1),
        ChildBudgetSnapshot(True, 0.5, 30.0),
    ],
)
async def test_budget_override_rejects_negative_nan_infinite_and_nonzero_usage(
    spawn_db: SpawnDatabase,
    budget: ChildBudgetSnapshot,
) -> None:
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="非法预算数值",
            budget=budget,
        )

    assert raised.value.reason == "invalid_child_budget"
    await _assert_no_receipts(manager, spawn_db.session_id)


@pytest.mark.parametrize(
    ("configured_max_steps", "budget"),
    [
        pytest.param(2_147_483_647, None, id="configured_limit"),
        pytest.param(
            10**400,
            ChildBudgetSnapshot(2_147_483_647, 0.5, 30.0),
            id="override",
        ),
    ],
)
async def test_spawn_rejects_step_limit_outside_trace_range_before_persisting(
    spawn_db: SpawnDatabase,
    configured_max_steps: int,
    budget: ChildBudgetSnapshot | None,
) -> None:
    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=_completed_runner,
        child_max_steps=configured_max_steps,
    )
    created: ChildReceipt | None = None

    try:
        with pytest.raises(SpawnRejectedError) as raised:
            created = await manager.spawn_agent(
                session_id=spawn_db.session_id,
                role="kb_explorer",
                task_brief="超出 trace Integer 范围",
                budget=budget,
            )
    finally:
        if created is not None:
            await manager.wait_agent(created.child_id)
            await manager.close_agent(created.child_id)

    assert raised.value.reason == "invalid_child_budget"
    assert raised.value.limit_name == "child_max_steps"
    await _assert_no_receipts(manager, spawn_db.session_id)


async def test_spawn_accepts_largest_trace_safe_step_limit(
    spawn_db: SpawnDatabase,
) -> None:
    largest_safe_step = 2_147_483_646
    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=_completed_runner,
        child_max_steps=largest_safe_step,
    )
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="最大安全 step",
        budget=ChildBudgetSnapshot(largest_safe_step, 0.5, 30.0),
    )

    try:
        terminal = await manager.wait_agent(child.child_id)
        assert terminal.status == "COMPLETED"
        assert terminal.budget.max_steps == largest_safe_step
    finally:
        await manager.close_agent(child.child_id)


async def test_two_children_can_be_running_at_the_same_time(
    spawn_db: SpawnDatabase,
) -> None:
    started: set[str] = set()
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def concurrent_runner(
        _db: AsyncSession,
        receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        started.add(receipt.child_id)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        return ChildRunResult(status="COMPLETED", result_summary=receipt.task_brief)

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=concurrent_runner,
        max_concurrent_children=2,
        max_total_child_cost_usd=2.0,
    )
    first = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="first",
    )
    second = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="ops_explorer",
        task_brief="second",
    )

    async with asyncio.timeout(1):
        await both_started.wait()
    running = await manager.list_agents(spawn_db.session_id)
    assert {receipt.status for receipt in running} == {"RUNNING"}
    assert started == {first.child_id, second.child_id}

    release.set()
    await asyncio.gather(
        manager.wait_agent(first.child_id),
        manager.wait_agent(second.child_id),
    )
    await manager.close_agent(first.child_id)
    await manager.close_agent(second.child_id)


async def test_sixth_active_child_is_rejected_immediately(
    spawn_db: SpawnDatabase,
) -> None:
    all_started = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def gated_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        nonlocal started
        started += 1
        if started == 5:
            all_started.set()
        await release.wait()
        return ChildRunResult(status="COMPLETED", result_summary="done")

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=gated_runner,
        max_concurrent_children=5,
        max_total_child_cost_usd=10.0,
    )
    receipts = [
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief=f"child-{index}",
            budget=ChildBudgetSnapshot(5, 0.0, 30.0),
        )
        for index in range(5)
    ]
    async with asyncio.timeout(1):
        await all_started.wait()

    async with asyncio.timeout(0.2):
        with pytest.raises(SpawnRejectedError) as raised:
            await manager.spawn_agent(
                session_id=spawn_db.session_id,
                role="kb_explorer",
                task_brief="sixth",
                budget=ChildBudgetSnapshot(5, 0.0, 30.0),
            )

    assert raised.value.reason == "max_concurrent_children"
    assert len(await manager.list_agents(spawn_db.session_id)) == 5
    release.set()
    await asyncio.gather(*(manager.wait_agent(item.child_id) for item in receipts))
    for receipt in receipts:
        await manager.close_agent(receipt.child_id)


async def test_completed_child_holds_slot_until_close(
    spawn_db: SpawnDatabase,
) -> None:
    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=_completed_runner,
        max_concurrent_children=1,
        max_total_child_cost_usd=10.0,
    )
    first = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="first",
    )
    completed = await manager.wait_agent(first.child_id)
    assert completed.status == "COMPLETED"

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="blocked until close",
        )

    assert raised.value.reason == "max_concurrent_children"
    await manager.close_agent(first.child_id)
    second = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="after close",
    )
    await manager.wait_agent(second.child_id)
    await manager.close_agent(second.child_id)


async def test_close_releases_its_owned_slot_exactly_once(
    spawn_db: SpawnDatabase,
) -> None:
    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=_completed_runner,
        max_concurrent_children=1,
        max_total_child_cost_usd=10.0,
    )
    first = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="first",
    )
    await manager.wait_agent(first.child_id)
    closed = await manager.close_agent(first.child_id)
    async with spawn_db.session_factory() as db:
        trace_count = len(await agent_trace_event_crud.list_for_trace(db, first.trace_id))

    repeated = await manager.close_agent(first.child_id)
    async with spawn_db.session_factory() as db:
        repeated_trace_count = len(
            await agent_trace_event_crud.list_for_trace(db, first.trace_id)
        )
    assert repeated == closed
    assert repeated_trace_count == trace_count

    second = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="second",
    )
    with pytest.raises(SpawnRejectedError):
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="slot was released twice",
        )
    await manager.wait_agent(second.child_id)
    await manager.close_agent(second.child_id)


async def test_wait_timeout_does_not_cancel_child(spawn_db: SpawnDatabase) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        started.set()
        await release.wait()
        return ChildRunResult(status="COMPLETED", result_summary="survived")

    manager = SpawnManager(spawn_db.session_factory, child_runner=gated_runner)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="wait",
    )
    await started.wait()

    with pytest.raises(ChildWaitTimeoutError) as raised:
        await manager.wait_agent(child.child_id, timeout_ms=1)

    assert raised.value.child_id == child.child_id
    assert raised.value.timeout_ms == 1
    assert (await manager.list_agents(spawn_db.session_id))[0].status == "RUNNING"
    release.set()
    completed = await manager.wait_agent(child.child_id)
    assert completed.status == "COMPLETED"
    assert completed.result_summary == "survived"
    await manager.close_agent(child.child_id)


async def test_wait_returns_persisted_terminal_receipt_without_local_task(
    spawn_db: SpawnDatabase,
) -> None:
    owner = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)
    child = await owner.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="persist",
    )
    await owner.wait_agent(child.child_id)
    restarted = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    persisted = await restarted.wait_agent(child.child_id, timeout_ms=1)

    assert persisted.status == "COMPLETED"
    assert persisted.child_id == child.child_id
    await owner.close_agent(child.child_id)


async def _persist_orphan_running(
    spawn_db: SpawnDatabase,
    *,
    child_id: str = "orphan-child",
) -> None:
    async with spawn_db.session_factory() as db:
        await agent_registry_crud.create(
            db,
            child_id=child_id,
            session_id=spawn_db.session_id,
            trace_id="orphan-trace",
            role_version="t09-v1",
            parent_agent_id=None,
            agent_path=f"/root/{child_id}",
            role="kb_explorer",
            model="local-chat",
            tools_allowlist=["kb_read"],
            sandbox_mode="read-only",
            task_brief="orphan",
            budget={"max_steps": 5, "max_cost_usd": 0.5},
        )
        await agent_registry_crud.transition_status(db, child_id, "SPAWNING")
        await agent_registry_crud.transition_status(db, child_id, "RUNNING")
        await db.commit()


async def test_wait_reports_runtime_unavailable_for_orphan_active_row(
    spawn_db: SpawnDatabase,
) -> None:
    await _persist_orphan_running(spawn_db)
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    with pytest.raises(ChildRuntimeUnavailableError) as raised:
        await manager.wait_agent("orphan-child", timeout_ms=1)

    assert raised.value.child_id == "orphan-child"
    await manager.close_agent("orphan-child")


async def test_send_input_only_appends_to_a_running_child(
    spawn_db: SpawnDatabase,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        started.set()
        await release.wait()
        return ChildRunResult(status="FAILED", result_summary="needs correction")

    manager = SpawnManager(spawn_db.session_factory, child_runner=gated_runner)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="initial brief",
    )
    await started.wait()

    running = await manager.send_input(child.child_id, "补充：只看网络章节")

    assert running.status == "RUNNING"
    async with spawn_db.session_factory() as db:
        root_messages = await agent_message_crud.list_for_agent(
            db, spawn_db.session_id, agent_id=None
        )
        child_messages = await agent_message_crud.list_for_agent(
            db, spawn_db.session_id, agent_id=child.child_id
        )
    assert root_messages == []
    assert [(message.role, message.content) for message in child_messages] == [
        ("user", "initial brief"),
        ("user", "补充：只看网络章节"),
    ]

    with pytest.raises(SpawnRejectedError) as blank:
        await manager.send_input(child.child_id, " \n")
    assert blank.value.reason == "blank_input"

    release.set()
    failed = await manager.wait_agent(child.child_id)
    assert failed.status == "FAILED"
    with pytest.raises(SpawnRejectedError) as terminal:
        await manager.send_input(child.child_id, "终态不能重开")
    assert terminal.value.reason == "child_not_running"
    await manager.close_agent(child.child_id)


async def test_close_cancels_running_child_and_is_idempotent(
    spawn_db: SpawnDatabase,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def cancellable_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    manager = SpawnManager(spawn_db.session_factory, child_runner=cancellable_runner)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="cancel",
    )
    await started.wait()

    first = await manager.close_agent(child.child_id)
    await cancelled.wait()
    async with spawn_db.session_factory() as db:
        trace_count = len(await agent_trace_event_crud.list_for_trace(db, child.trace_id))
    second = await manager.close_agent(child.child_id)
    async with spawn_db.session_factory() as db:
        repeated_trace_count = len(
            await agent_trace_event_crud.list_for_trace(db, child.trace_id)
        )

    assert first.status == "CLOSED"
    assert first.force_closed is False
    assert second == first
    assert repeated_trace_count == trace_count


async def test_close_force_detaches_child_that_swallows_cancellation(
    spawn_db: SpawnDatabase,
) -> None:
    started = asyncio.Event()
    cancellation_swallowed = asyncio.Event()
    release = asyncio.Event()
    late_finished = asyncio.Event()

    async def hung_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation_swallowed.set()
            await release.wait()
        late_finished.set()
        return ChildRunResult(status="COMPLETED", result_summary="late overwrite")

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=hung_runner,
        close_timeout_seconds=0.01,
    )
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="hung",
    )
    await started.wait()

    async with asyncio.timeout(0.2):
        closed = await manager.close_agent(child.child_id)

    assert cancellation_swallowed.is_set()
    assert closed.status == "CLOSED"
    assert closed.force_closed is True
    assert closed.result_summary is None
    async with spawn_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
    assert [event.control for event in events] == ["REQUESTED", "CANCELLED", "CLOSED"]
    assert [event.span_type for event in events] == ["spawn", "agent", "close"]

    release.set()
    async with asyncio.timeout(1):
        await late_finished.wait()
    await asyncio.sleep(0)
    persisted = (await manager.list_agents(spawn_db.session_id))[0]
    assert persisted.status == "CLOSED"
    assert persisted.result_summary is None
    assert persisted.force_closed is True


async def test_force_detach_releases_task_and_budget_ownership(
    spawn_db: SpawnDatabase,
) -> None:
    started = asyncio.Event()
    cancellation_swallowed = asyncio.Event()
    release = asyncio.Event()
    late_finished = asyncio.Event()

    async def permanently_hung_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        budget: Budget,
    ) -> ChildRunResult:
        budget.record_step(0.1)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation_swallowed.set()
            await release.wait()
        late_finished.set()
        return ChildRunResult(status="COMPLETED", result_summary="late")

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=permanently_hung_runner,
        close_timeout_seconds=0.01,
    )
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="detach local ownership",
    )
    await started.wait()

    try:
        closed = await manager.close_agent(child.child_id)

        assert cancellation_swallowed.is_set()
        assert closed.status == "CLOSED"
        assert closed.force_closed is True
        assert child.child_id not in manager._tasks
        assert child.child_id not in manager._active_budgets
        assert not late_finished.is_set()
    finally:
        release.set()
        async with asyncio.timeout(1):
            await late_finished.wait()


async def test_close_parent_closes_descendants_deepest_first(
    spawn_db: SpawnDatabase,
) -> None:
    started: set[str] = set()
    all_started = asyncio.Event()

    async def cancellable_runner(
        _db: AsyncSession,
        receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        started.add(receipt.child_id)
        if len(started) == 2:
            all_started.set()
        await asyncio.Event().wait()
        return ChildRunResult(status="COMPLETED", result_summary="unreachable")

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=cancellable_runner,
        max_concurrent_children=2,
        max_total_child_cost_usd=2.0,
    )
    parent = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="investigator",
        task_brief="parent",
        trace_id="cascade-trace",
    )
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="reviewer",
        task_brief="child",
        trace_id="cascade-trace",
        parent_agent_id=parent.child_id,
    )
    await all_started.wait()

    closed_parent = await manager.close_agent(parent.child_id)

    receipts = await manager.list_agents(spawn_db.session_id)
    assert {receipt.status for receipt in receipts} == {"CLOSED"}
    assert closed_parent.child_id == parent.child_id
    async with spawn_db.session_factory() as db:
        close_ids = list(
            (
                await db.execute(
                    select(AgentTraceEvent.agent_id)
                    .where(
                        AgentTraceEvent.trace_id == "cascade-trace",
                        AgentTraceEvent.span_type == "close",
                    )
                    .order_by(AgentTraceEvent.id.asc())
                )
            ).scalars()
        )
    assert close_ids == [child.child_id, parent.child_id]


async def test_child_lookup_errors_are_typed_and_safe(
    spawn_db: SpawnDatabase,
) -> None:
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)

    for operation in (
        manager.wait_agent("missing"),
        manager.send_input("missing", "hello"),
        manager.close_agent("missing"),
    ):
        with pytest.raises(ChildNotFoundError) as raised:
            await operation
        assert raised.value.child_id == "missing"
        assert "missing" not in str(raised.value)


async def test_each_child_uses_a_session_isolated_from_caller_and_siblings(
    spawn_db: SpawnDatabase,
) -> None:
    caller_session = spawn_db.session_factory()
    child_sessions: dict[str, AsyncSession] = {}
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def inspect_runner(
        db: AsyncSession,
        receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        child_sessions[receipt.child_id] = db
        assert receipt.status == "RUNNING"
        if len(child_sessions) == 2:
            both_started.set()
        await release.wait()
        return ChildRunResult(status="COMPLETED", result_summary="done")

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=inspect_runner,
        max_concurrent_children=2,
        max_total_child_cost_usd=2.0,
    )
    try:
        first = await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="first",
        )
        second = await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="ops_explorer",
            task_brief="second",
        )
        await both_started.wait()

        assert len({id(session) for session in child_sessions.values()}) == 2
        assert all(session is not caller_session for session in child_sessions.values())

        release.set()
        await manager.wait_agent(first.child_id)
        await manager.wait_agent(second.child_id)
        await manager.close_agent(first.child_id)
        await manager.close_agent(second.child_id)
    finally:
        await caller_session.close()


@pytest.mark.parametrize(
    ("run_result", "expected_status", "expected_error_class"),
    [
        (ChildRunResult("COMPLETED", "done", ("a.txt",)), "COMPLETED", None),
        (ChildRunResult("FAILED", None, error_class="model"), "FAILED", "model"),
        (ChildRunResult("FAILED", None, error_class="tool"), "FAILED", "tool"),
        (
            ChildRunResult("FAILED", None, error_class="policy_reject"),
            "FAILED",
            "policy_reject",
        ),
        (ChildRunResult("FAILED", None, error_class="infra"), "FAILED", "infra"),
    ],
)
async def test_runner_result_maps_terminal_status_budget_and_safe_trace(
    spawn_db: SpawnDatabase,
    run_result: ChildRunResult,
    expected_status: str,
    expected_error_class: str | None,
) -> None:
    async def measured_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        budget: Budget,
    ) -> ChildRunResult:
        budget.record_step(0.125)
        return run_result

    manager = SpawnManager(spawn_db.session_factory, child_runner=measured_runner)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="measure",
        trace_id="safe-trace",
        budget=ChildBudgetSnapshot(5, 0.5, 30.0),
    )

    terminal = await manager.wait_agent(child.child_id)

    assert terminal.status == expected_status
    assert terminal.budget == ChildBudgetSnapshot(5, 0.5, 30.0, 1, 0.125)
    assert terminal.artifacts == run_result.artifacts
    async with spawn_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, "safe-trace")
    assert [event.trace_id for event in events] == ["safe-trace", "safe-trace"]
    assert [event.span_type for event in events] == ["spawn", "agent"]
    assert events[-1].control == expected_status
    assert events[-1].error_class == expected_error_class
    assert events[-1].cost_usd == 0.125
    await manager.close_agent(child.child_id)


async def test_wall_timeout_persists_failed_budget_exceeded(
    spawn_db: SpawnDatabase,
) -> None:
    started = asyncio.Event()

    async def never_finishes(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        started.set()
        await asyncio.Event().wait()
        return ChildRunResult(status="COMPLETED", result_summary="unreachable")

    manager = SpawnManager(spawn_db.session_factory, child_runner=never_finishes)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="wall timeout",
        budget=ChildBudgetSnapshot(5, 0.5, 0.01),
    )
    await started.wait()

    terminal = await manager.wait_agent(child.child_id)

    assert terminal.status == "FAILED"
    assert terminal.result_summary is None
    async with spawn_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
    assert events[-1].control == "FAILED"
    assert events[-1].error_class == "budget_exceeded"
    await manager.close_agent(child.child_id)


async def test_wall_timeout_wins_with_budget_exceeded_when_runner_swallows_cancellation(
    spawn_db: SpawnDatabase,
) -> None:
    async def swallows_wall_cancellation(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return ChildRunResult(
                status="COMPLETED",
                result_summary="late completion",
            )

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=swallows_wall_cancellation,
    )
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="swallowed wall cancellation",
        budget=ChildBudgetSnapshot(5, 0.5, 0.01),
    )

    terminal = await manager.wait_agent(child.child_id)

    try:
        assert terminal.status == "FAILED"
        assert terminal.result_summary is None
        async with spawn_db.session_factory() as db:
            events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
        assert events[-1].error_class == "budget_exceeded"
    finally:
        await manager.close_agent(child.child_id)


async def test_default_runner_maps_budget_exceeded_to_failed_budget_exceeded(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched = False

    async def expensive_tool_chat(
        _model_key: str,
        _messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> ChatResult:
        del tools
        return ChatResult(
            content=None,
            tool_calls=[
                ToolCall(
                    id="read-1",
                    name="kb_read",
                    arguments='{"path":"network.md"}',
                )
            ],
            finish_reason="tool_calls",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.60,
        )

    async def must_not_dispatch(
        _path: str,
        *,
        offset: int = 0,
        limit: int | None = 4000,
    ) -> ToolResult:
        nonlocal dispatched
        dispatched = True
        del offset, limit
        return ToolResult(control="ok", content="unexpected")

    monkeypatch.setattr("app.agent.tool_dispatch.kb_read", must_not_dispatch)
    manager = SpawnManager(spawn_db.session_factory, chat_fn=expensive_tool_chat)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="超预算",
        budget=ChildBudgetSnapshot(5, 0.50, 30.0),
    )

    terminal = await manager.wait_agent(child.child_id)

    assert terminal.status == "FAILED"
    assert terminal.result_summary is None
    assert dispatched is False
    async with spawn_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
    budget_event = events[-1]
    assert budget_event.control == "FAILED"
    assert budget_event.error_class == "budget_exceeded"
    await manager.close_agent(child.child_id)


async def test_default_runner_maps_llm_error_to_failed_model(
    spawn_db: SpawnDatabase,
) -> None:
    async def llm_error_chat(
        _model_key: str,
        _messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> ChatResult:
        del tools
        raise LlmRequestError("token=do-not-persist")

    manager = SpawnManager(spawn_db.session_factory, chat_fn=llm_error_chat)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="模型失败",
    )

    terminal = await manager.wait_agent(child.child_id)

    assert terminal.status == "FAILED"
    assert terminal.result_summary is None
    async with spawn_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
    llm_event = events[-1]
    assert llm_event.control == "FAILED"
    assert llm_event.error_class == "model"
    await manager.close_agent(child.child_id)


@pytest.mark.parametrize(
    ("exception", "error_class"),
    [
        (LlmRequestError("token=do-not-persist"), "model"),
        (RuntimeError("postgresql://user:password@secret"), "infra"),
    ],
)
async def test_escaped_runner_exception_uses_safe_failure_mapping(
    spawn_db: SpawnDatabase,
    exception: Exception,
    error_class: str,
) -> None:
    async def raises(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        raise exception

    manager = SpawnManager(spawn_db.session_factory, child_runner=raises)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="safe exception",
    )

    terminal = await manager.wait_agent(child.child_id)

    assert terminal.status == "FAILED"
    assert terminal.result_summary is None
    assert str(exception) not in (terminal.result_summary or "")
    async with spawn_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
    assert events[-1].error_class == error_class
    await manager.close_agent(child.child_id)


async def test_failed_child_transaction_uses_fresh_session_for_terminal_fallback(
    spawn_db: SpawnDatabase,
) -> None:
    poisoned_session: AsyncSession | None = None

    async def poison_transaction(
        db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        nonlocal poisoned_session
        poisoned_session = db
        db.add(
            AgentSession(
                id=spawn_db.session_id,
                user_id=1,
                title="duplicate primary key",
                status="active",
            )
        )
        await db.flush()
        return ChildRunResult(status="COMPLETED", result_summary="unreachable")

    manager = SpawnManager(spawn_db.session_factory, child_runner=poison_transaction)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="poison",
    )

    terminal = await manager.wait_agent(child.child_id)

    assert poisoned_session is not None
    assert terminal.status == "FAILED"
    assert terminal.result_summary is None
    async with spawn_db.session_factory() as db:
        persisted = await agent_registry_crud.get(db, child.child_id)
        events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
    assert persisted is not None and persisted.status == "FAILED"
    assert events[-1].error_class == "infra"
    await manager.close_agent(child.child_id)


async def test_default_runner_completes_with_injected_chat_fn(
    spawn_db: SpawnDatabase,
) -> None:
    observed: list[tuple[str, list[ChatMessage], list[dict[str, object]] | None]] = []

    async def fake_chat(
        model_key: str,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> ChatResult:
        observed.append((model_key, messages, tools))
        return ChatResult(
            content="final evidence",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.02,
        )

    manager = SpawnManager(spawn_db.session_factory, chat_fn=fake_chat)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="读取 runbook",
        budget=ChildBudgetSnapshot(5, 0.5, 30.0),
    )

    terminal = await manager.wait_agent(child.child_id)

    assert terminal.status == "COMPLETED"
    assert terminal.result_summary == "final evidence"
    assert terminal.budget.steps_used == 1
    assert terminal.budget.cost_used_usd == 0.02
    assert len(observed) == 1
    model_key, messages, tools = observed[0]
    assert model_key == "local-chat"
    assert messages[0].role == "system"
    assert messages[1] == ChatMessage(role="user", content="读取 runbook")
    assert tools is not None
    assert {schema["function"]["name"] for schema in tools} == {
        "kb_glob",
        "kb_grep",
        "kb_read",
        "kb_semantic_search",
    }
    await manager.close_agent(child.child_id)


async def test_default_runner_feeds_tool_failed_back_for_correction(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    histories: list[list[ChatMessage]] = []

    async def failed_read(
        _path: str,
        *,
        offset: int = 0,
        limit: int | None = 4000,
    ) -> ToolResult:
        del offset, limit
        return ToolResult(control="failed", content="工具 'kb_read' 执行失败: OSError")

    async def correcting_chat(
        _model_key: str,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> ChatResult:
        del tools
        histories.append(messages)
        if len(histories) == 1:
            return ChatResult(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="read-1",
                        name="kb_read",
                        arguments='{"path":"network.md"}',
                    )
                ],
                finish_reason="tool_calls",
                prompt_tokens=2,
                completion_tokens=2,
            )
        return ChatResult(
            content="corrected final",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=2,
            completion_tokens=2,
        )

    monkeypatch.setattr("app.agent.tool_dispatch.kb_read", failed_read)
    manager = SpawnManager(spawn_db.session_factory, chat_fn=correcting_chat)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="纠错",
    )

    terminal = await manager.wait_agent(child.child_id)

    assert terminal.status == "COMPLETED"
    assert terminal.result_summary == "corrected final"
    assert terminal.budget.steps_used == 2
    assert len(histories) == 2
    from app.agent.compaction import TOOL_RESULT_UNTRUSTED_PREFIX

    assert histories[1][-1] == ChatMessage(
        role="tool",
        content=TOOL_RESULT_UNTRUSTED_PREFIX + "工具 'kb_read' 执行失败: OSError",
        tool_call_id="read-1",
    )
    await manager.close_agent(child.child_id)


async def test_default_runner_maps_clarification_to_failed_policy_reject(
    spawn_db: SpawnDatabase,
) -> None:
    async def invalid_tool_chat(
        _model_key: str,
        _messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> ChatResult:
        del tools
        return ChatResult(
            content=None,
            tool_calls=[ToolCall(id="read-1", name="kb_read", arguments="{}")],
            finish_reason="tool_calls",
            prompt_tokens=2,
            completion_tokens=2,
        )

    manager = SpawnManager(spawn_db.session_factory, chat_fn=invalid_tool_chat)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="需要澄清",
    )

    terminal = await manager.wait_agent(child.child_id)

    assert terminal.status == "FAILED"
    assert terminal.result_summary is None
    async with spawn_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
    assert events[-1].control == "FAILED"
    assert events[-1].error_class == "policy_reject"
    await manager.close_agent(child.child_id)


async def test_create_task_failure_is_compensated_and_slot_is_reusable(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=_completed_runner,
        max_concurrent_children=1,
        max_total_child_cost_usd=10.0,
    )

    def fail_create_task(*_args: object, **_kwargs: object) -> asyncio.Task[None]:
        raise RuntimeError("task factory secret")

    with monkeypatch.context() as patcher:
        patcher.setattr("app.agent.spawn.manager._create_child_task", fail_create_task)
        with pytest.raises(ChildRuntimeUnavailableError) as raised:
            await manager.spawn_agent(
                session_id=spawn_db.session_id,
                role="kb_explorer",
                task_brief="task creation failure",
            )

    receipts = await manager.list_agents(spawn_db.session_id)
    assert len(receipts) == 1
    assert receipts[0].child_id == raised.value.child_id
    assert receipts[0].status == "CLOSED"
    assert receipts[0].result_summary is None
    async with spawn_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, receipts[0].trace_id)
    assert [event.control for event in events] == ["REQUESTED", "FAILED", "CLOSED"]
    assert events[1].error_class == "infra"

    replacement = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="slot was released",
    )
    await manager.wait_agent(replacement.child_id)
    await manager.close_agent(replacement.child_id)


async def test_default_runner_maps_escaped_dispatch_exception_to_tool(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def escaped_dispatch(
        _name: str,
        _arguments: dict[str, object],
    ) -> ToolResult:
        raise RuntimeError("tool backend secret")

    def raising_dispatcher(
        _db: AsyncSession,
        _allowlist: tuple[str, ...],
    ) -> object:
        return escaped_dispatch

    async def tool_call_chat(
        _model_key: str,
        _messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> ChatResult:
        del tools
        return ChatResult(
            content=None,
            tool_calls=[ToolCall(id="tool-1", name="kb_read", arguments="{}")],
            finish_reason="tool_calls",
            prompt_tokens=1,
            completion_tokens=1,
        )

    monkeypatch.setattr("app.agent.spawn.manager.build_tool_dispatcher", raising_dispatcher)
    manager = SpawnManager(spawn_db.session_factory, chat_fn=tool_call_chat)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="escaping tool exception",
    )

    terminal = await manager.wait_agent(child.child_id)

    try:
        assert terminal.status == "FAILED"
        assert terminal.result_summary is None
        async with spawn_db.session_factory() as db:
            events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
        assert events[-1].error_class == "tool"
    finally:
        await manager.close_agent(child.child_id)


async def test_running_transition_failure_uses_fresh_terminal_transaction(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_transition = agent_registry_crud.transition_status
    failed_once = False

    async def fail_running_once(
        db: AsyncSession,
        child_id: str,
        target_status: str,
        **kwargs: object,
    ) -> object:
        nonlocal failed_once
        if target_status == "RUNNING" and not failed_once:
            failed_once = True
            raise RuntimeError("running transition failed")
        return await original_transition(db, child_id, target_status, **kwargs)

    monkeypatch.setattr(agent_registry_crud, "transition_status", fail_running_once)
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="running transition",
    )

    terminal = await manager.wait_agent(child.child_id)

    try:
        assert failed_once is True
        assert terminal.status == "FAILED"
        assert terminal.result_summary is None
        async with spawn_db.session_factory() as db:
            events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
        assert events[-1].error_class == "infra"
    finally:
        await manager.close_agent(child.child_id)


async def test_terminal_transaction_failure_falls_back_to_failed_infra(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_transition = agent_registry_crud.transition_status
    failed_once = False

    async def fail_completed_once(
        db: AsyncSession,
        child_id: str,
        target_status: str,
        **kwargs: object,
    ) -> object:
        nonlocal failed_once
        if target_status == "COMPLETED" and not failed_once:
            failed_once = True
            raise RuntimeError("terminal transaction failed")
        return await original_transition(db, child_id, target_status, **kwargs)

    monkeypatch.setattr(agent_registry_crud, "transition_status", fail_completed_once)
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="terminal fallback",
    )

    try:
        terminal = await manager.wait_agent(child.child_id)
        assert failed_once is True
        assert terminal.status == "FAILED"
        assert terminal.result_summary is None
        async with spawn_db.session_factory() as db:
            events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
        assert events[-1].error_class == "infra"
    finally:
        await manager.close_agent(child.child_id)


class _BlockingSessionExit:
    def __init__(
        self,
        session: AsyncSession,
        entered: asyncio.Event,
    ) -> None:
        self._session = session
        self._entered = entered

    async def __aenter__(self) -> AsyncSession:
        return await self._session.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self._entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await self._session.__aexit__(exc_type, exc, traceback)


class _BlockFirstSessionExitFactory:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        entered: asyncio.Event,
    ) -> None:
        self._session_factory = session_factory
        self._entered = entered
        self._calls = 0

    def __call__(self) -> AsyncSession | _BlockingSessionExit:
        self._calls += 1
        session = self._session_factory()
        if self._calls == 1:
            return _BlockingSessionExit(session, self._entered)
        return session


def _block_next_commit_after_durable_write(
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit_number: int = 1,
) -> asyncio.Event:
    original_commit = AsyncSession.commit
    durable_commit_finished = asyncio.Event()
    commit_count = 0

    async def commit_then_block(session: AsyncSession) -> None:
        nonlocal commit_count
        await original_commit(session)
        commit_count += 1
        if commit_count == commit_number:
            durable_commit_finished.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(AsyncSession, "commit", commit_then_block)
    return durable_commit_finished


async def test_cancelled_durable_force_close_releases_all_local_ownership(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cancellation_swallowed = asyncio.Event()
    release = asyncio.Event()
    late_finished = asyncio.Event()

    async def permanently_hung_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        budget: Budget,
    ) -> ChildRunResult:
        budget.record_step(0.1)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation_swallowed.set()
            await release.wait()
        late_finished.set()
        return ChildRunResult(status="COMPLETED", result_summary="late")

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=permanently_hung_runner,
        close_timeout_seconds=0.01,
    )
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="cancel durable force close",
    )
    await started.wait()
    durable_close_finished = _block_next_commit_after_durable_write(
        monkeypatch,
        commit_number=2,
    )
    close_task = asyncio.create_task(manager.close_agent(child.child_id))

    try:
        async with asyncio.timeout(1):
            await durable_close_finished.wait()
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task

        persisted = (await manager.list_agents(spawn_db.session_id))[0]
        assert cancellation_swallowed.is_set()
        assert persisted.status == "CLOSED"
        assert persisted.force_closed is True
        assert child.child_id not in manager._runtime(
            spawn_db.session_id
        ).held_child_ids
        assert child.child_id not in manager._tasks
        assert child.child_id not in manager._active_budgets
        assert not late_finished.is_set()
    finally:
        release.set()
        async with asyncio.timeout(1):
            await late_finished.wait()


async def test_spawn_reconciles_cancelled_durable_commit_and_releases_slot_once(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable_commit_finished = _block_next_commit_after_durable_write(monkeypatch)
    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=_completed_runner,
        max_concurrent_children=1,
        max_total_child_cost_usd=10.0,
    )
    spawn_task = asyncio.create_task(
        manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="ambiguous durable spawn commit",
        )
    )
    async with asyncio.timeout(1):
        await durable_commit_finished.wait()
    spawn_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await spawn_task

    receipts = await manager.list_agents(spawn_db.session_id)
    replacement: ChildReceipt | None = None
    try:
        assert len(receipts) == 1
        failed_spawn = receipts[0]
        assert failed_spawn.status == "CLOSED"
        assert failed_spawn.result_summary is None
        assert failed_spawn.child_id not in manager._runtime(
            spawn_db.session_id
        ).held_child_ids
        async with spawn_db.session_factory() as db:
            events = await agent_trace_event_crud.list_for_trace(
                db, failed_spawn.trace_id
            )
        assert [event.control for event in events] == [
            "REQUESTED",
            "FAILED",
            "CLOSED",
        ]
        assert events[1].error_class == "infra"

        replacement = await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="replacement owns the only slot",
        )
        with pytest.raises(SpawnRejectedError) as raised:
            await manager.spawn_agent(
                session_id=spawn_db.session_id,
                role="kb_explorer",
                task_brief="slot must not be released twice",
            )
        assert raised.value.reason == "max_concurrent_children"
        await manager.wait_agent(replacement.child_id)
    finally:
        for receipt in await manager.list_agents(spawn_db.session_id):
            if receipt.status != "CLOSED":
                await manager.close_agent(receipt.child_id)


async def test_close_reconciles_cancelled_durable_commit_and_releases_slot_once(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=_completed_runner,
        max_concurrent_children=1,
        max_total_child_cost_usd=10.0,
    )
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="ambiguous durable close commit",
    )
    await manager.wait_agent(child.child_id)
    durable_commit_finished = _block_next_commit_after_durable_write(monkeypatch)
    close_task = asyncio.create_task(manager.close_agent(child.child_id))
    async with asyncio.timeout(1):
        await durable_commit_finished.wait()
    close_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    persisted = (await manager.list_agents(spawn_db.session_id))[0]
    replacement: ChildReceipt | None = None
    try:
        assert persisted.status == "CLOSED"
        assert persisted.child_id not in manager._runtime(
            spawn_db.session_id
        ).held_child_ids
        async with spawn_db.session_factory() as db:
            events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
        assert [event.control for event in events] == [
            "REQUESTED",
            "COMPLETED",
            "CLOSED",
        ]

        replacement = await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="replacement owns the released slot",
        )
        with pytest.raises(SpawnRejectedError) as raised:
            await manager.spawn_agent(
                session_id=spawn_db.session_id,
                role="kb_explorer",
                task_brief="close must not release twice",
            )
        assert raised.value.reason == "max_concurrent_children"
        await manager.wait_agent(replacement.child_id)
    finally:
        for receipt in await manager.list_agents(spawn_db.session_id):
            if receipt.status != "CLOSED":
                await manager.close_agent(receipt.child_id)


async def test_spawn_cancellation_after_commit_compensates_and_reuses_slot(
    spawn_db: SpawnDatabase,
) -> None:
    exit_entered = asyncio.Event()
    blocking_factory = _BlockFirstSessionExitFactory(
        spawn_db.session_factory, exit_entered
    )
    manager = SpawnManager(  # type: ignore[arg-type]
        blocking_factory,
        child_runner=_completed_runner,
        max_concurrent_children=1,
        max_total_child_cost_usd=10.0,
    )
    spawn_task = asyncio.create_task(
        manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="cancel after commit",
        )
    )
    await exit_entered.wait()
    spawn_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await spawn_task

    receipts = await manager.list_agents(spawn_db.session_id)
    replacement: ChildReceipt | None = None
    try:
        assert len(receipts) == 1
        assert receipts[0].status == "CLOSED"
        async with spawn_db.session_factory() as db:
            events = await agent_trace_event_crud.list_for_trace(
                db, receipts[0].trace_id
            )
        assert [event.control for event in events] == [
            "REQUESTED",
            "FAILED",
            "CLOSED",
        ]
        assert events[1].error_class == "infra"

        replacement = await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="kb_explorer",
            task_brief="slot reusable",
        )
        await manager.wait_agent(replacement.child_id)
    finally:
        for receipt in await manager.list_agents(spawn_db.session_id):
            if receipt.status != "CLOSED":
                await manager.close_agent(receipt.child_id)


async def test_wait_rechecks_terminal_receipt_after_done_callback_removes_task(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    callback_finished = asyncio.Event()

    async def gated_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        started.set()
        await release.wait()
        return ChildRunResult(status="COMPLETED", result_summary="done")

    manager = SpawnManager(spawn_db.session_factory, child_runner=gated_runner)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="wait race",
    )
    await started.wait()
    owned_task = manager._tasks[child.child_id]
    owned_task.add_done_callback(lambda _done: callback_finished.set())
    original_get = manager._get_receipt
    first_lookup = True

    async def return_one_stale_active_snapshot(child_id: str) -> ChildReceipt:
        nonlocal first_lookup
        receipt = await original_get(child_id)
        if first_lookup:
            first_lookup = False
            release.set()
            await callback_finished.wait()
        return receipt

    monkeypatch.setattr(manager, "_get_receipt", return_one_stale_active_snapshot)

    try:
        terminal = await manager.wait_agent(child.child_id)
        assert terminal.status == "COMPLETED"
        assert terminal.result_summary == "done"
    finally:
        await manager.close_agent(child.child_id)


async def test_force_detach_persists_live_budget_usage(
    spawn_db: SpawnDatabase,
) -> None:
    usage_recorded = asyncio.Event()
    cancellation_swallowed = asyncio.Event()
    release = asyncio.Event()
    late_finished = asyncio.Event()

    async def measured_hung_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        budget: Budget,
    ) -> ChildRunResult:
        budget.record_step(0.25)
        usage_recorded.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation_swallowed.set()
            await release.wait()
        late_finished.set()
        return ChildRunResult(status="COMPLETED", result_summary="late")

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=measured_hung_runner,
        close_timeout_seconds=0.01,
    )
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="preserve live budget",
        budget=ChildBudgetSnapshot(5, 0.5, 30.0),
    )
    await usage_recorded.wait()

    closed = await manager.close_agent(child.child_id)

    try:
        assert cancellation_swallowed.is_set()
        assert closed.status == "CLOSED"
        assert closed.force_closed is True
        assert closed.budget == ChildBudgetSnapshot(5, 0.5, 30.0, 1, 0.25)
        async with spawn_db.session_factory() as db:
            events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
        cancelled = [event for event in events if event.control == "CANCELLED"]
        assert len(cancelled) == 1
        assert cancelled[0].cost_usd == 0.25
    finally:
        release.set()
        async with asyncio.timeout(1):
            await late_finished.wait()


async def test_cascade_close_rejects_nested_spawn_after_descendant_snapshot(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_started = asyncio.Event()

    async def cancellable_runner(
        _db: AsyncSession,
        receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        if receipt.role == "investigator":
            parent_started.set()
        await asyncio.Event().wait()
        return ChildRunResult(status="COMPLETED", result_summary="unreachable")

    manager = SpawnManager(
        spawn_db.session_factory,
        child_runner=cancellable_runner,
        max_concurrent_children=2,
        max_total_child_cost_usd=2.0,
    )
    parent = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="investigator",
        task_brief="closing parent",
    )
    await parent_started.wait()
    close_snapshot_taken = asyncio.Event()
    allow_close = asyncio.Event()
    original_close_one = manager._close_one

    async def pause_before_parent_close(child_id: str) -> ChildReceipt:
        if child_id == parent.child_id:
            close_snapshot_taken.set()
            await allow_close.wait()
        return await original_close_one(child_id)

    monkeypatch.setattr(manager, "_close_one", pause_before_parent_close)
    close_task = asyncio.create_task(manager.close_agent(parent.child_id))
    await close_snapshot_taken.wait()
    unexpected_child: ChildReceipt | None = None

    try:
        with pytest.raises(SpawnRejectedError) as raised:
            unexpected_child = await manager.spawn_agent(
                session_id=spawn_db.session_id,
                role="reviewer",
                task_brief="must not enter closing subtree",
                parent_agent_id=parent.child_id,
            )
        assert raised.value.reason == "parent_closing"
    finally:
        allow_close.set()
        await close_task
        if unexpected_child is not None:
            await manager.close_agent(unexpected_child.child_id)

    assert {receipt.status for receipt in await manager.list_agents(spawn_db.session_id)} == {
        "CLOSED"
    }


async def test_inner_timeout_error_is_infra_not_wall_timeout(
    spawn_db: SpawnDatabase,
) -> None:
    async def inner_timeout(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        raise TimeoutError("inner dependency timeout")

    manager = SpawnManager(spawn_db.session_factory, child_runner=inner_timeout)
    child = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="kb_explorer",
        task_brief="inner timeout",
        budget=ChildBudgetSnapshot(5, 0.5, 30.0),
    )

    terminal = await manager.wait_agent(child.child_id)

    try:
        assert terminal.status == "FAILED"
        assert terminal.result_summary is None
        async with spawn_db.session_factory() as db:
            events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
        assert events[-1].error_class == "infra"
    finally:
        await manager.close_agent(child.child_id)


async def test_cancelled_close_cleans_closing_admission_gate(
    spawn_db: SpawnDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def cancellable_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        started.set()
        await asyncio.Event().wait()
        return ChildRunResult(status="COMPLETED", result_summary="unreachable")

    manager = SpawnManager(spawn_db.session_factory, child_runner=cancellable_runner)
    parent = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="investigator",
        task_brief="cancel close cleanup",
    )
    await started.wait()
    runtime = manager._runtime(spawn_db.session_id)
    close_body_finished = asyncio.Event()
    original_close_one = manager._close_one

    async def close_then_hold_cleanup_lock(child_id: str) -> ChildReceipt:
        closed = await original_close_one(child_id)
        await runtime.lock.acquire()
        close_body_finished.set()
        return closed

    monkeypatch.setattr(manager, "_close_one", close_then_hold_cleanup_lock)
    close_task = asyncio.create_task(manager.close_agent(parent.child_id))
    await close_body_finished.wait()
    close_task.cancel()
    runtime.lock.release()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=spawn_db.session_id,
            role="reviewer",
            task_brief="parent is already closed",
            parent_agent_id=parent.child_id,
        )
    assert raised.value.reason == "parent_closed"


class FakeSpawnEventPublisher:
    """记录已发布的子 Agent 回执，供断言使用。"""

    def __init__(self) -> None:
        self.receipts: list[ChildReceipt] = []

    async def publish_child_status(self, receipt: ChildReceipt) -> None:
        self.receipts.append(receipt)


class RaisingSpawnEventPublisher:
    """模拟发布器失败，用于验证不影响子任务状态。"""

    async def publish_child_status(self, receipt: ChildReceipt) -> None:
        del receipt
        raise RuntimeError("publish failed")


@pytest_asyncio.fixture
def fake_publisher() -> FakeSpawnEventPublisher:
    return FakeSpawnEventPublisher()


@pytest_asyncio.fixture
async def spawn_manager(
    spawn_db: SpawnDatabase,
    fake_publisher: FakeSpawnEventPublisher,
) -> SpawnManager:
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)
    manager.set_event_publisher(fake_publisher)
    return manager


async def test_spawn_manager_publishes_durable_statuses(
    spawn_manager: SpawnManager,
    fake_publisher: FakeSpawnEventPublisher,
    spawn_db: SpawnDatabase,
) -> None:
    receipt = await spawn_manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="ops_explorer",
        task_brief="检查资产 42",
    )
    await spawn_manager.wait_agent(receipt.child_id, timeout_ms=1000)
    assert [item.status for item in fake_publisher.receipts] == [
        "SPAWNING",
        "RUNNING",
        "COMPLETED",
    ]
    assert fake_publisher.receipts[-1].child_id == receipt.child_id
    await spawn_manager.close_agent(receipt.child_id)


async def test_spawn_manager_survives_failing_publisher(
    spawn_db: SpawnDatabase,
) -> None:
    """发布器异常不应回滚子任务或导致 spawn 失败。"""
    manager = SpawnManager(spawn_db.session_factory, child_runner=_completed_runner)
    manager.set_event_publisher(RaisingSpawnEventPublisher())

    receipt = await manager.spawn_agent(
        session_id=spawn_db.session_id,
        role="ops_explorer",
        task_brief="检查资产 42",
    )
    completed = await manager.wait_agent(receipt.child_id, timeout_ms=1000)
    assert completed.status == "COMPLETED"
    await manager.close_agent(receipt.child_id)
