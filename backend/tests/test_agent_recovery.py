"""SpawnManager 启动对账（reconcile_startup）与回执 GC 的测试。

实现流程：
1. 用独立 SQLite 文件 + 真实 registry/trace CRUD 直接造出「进程重启后」的孤儿行——
   REQUESTED/SPAWNING/RUNNING 活跃态和 COMPLETED/FAILED/CANCELLED 终态都不经过
   一个真正跑过它们的 SpawnManager，模拟旧进程崩溃、新进程用一个全新 SpawnManager 启动。
2. `reconcile_startup()` 证明：全新 manager 的 `held_child_ids`/`_tasks` 都是空的，
   所以它绝不能为孤儿行调用 `slots.release()`（否则 BoundedSemaphore 会因为
   「释放次数超过持有次数」直接抛 ValueError）；同时孤儿行确实被合法迁移到
   CANCELLED/CLOSED 并打上 error_class="infra"（因为进程确实丢失了运行时所有权）。
3. `collect_expired_receipts(now=...)` 证明：只有「终态 + 早于 TTL」的行会被回收；
   本进程真正持有的槽位会被正确释放，回执/消息不会被物理删除。
4. `shutdown()` 证明：本进程本地仍在跑的 child 会被取消并关闭，且可以安全调用两次。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agent.budget import Budget
from app.agent.hitl_execution import reconcile_executing_proposals
from app.agent.spawn import ChildReceipt, ChildRunResult, SpawnManager, SpawnRejectedError
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models import Base
from app.models.agent_session import AgentSession
from app.models.user import User


@dataclass(slots=True)
class RecoveryDatabase:
    session_factory: async_sessionmaker[AsyncSession]
    session_id: int


@pytest_asyncio.fixture
async def recovery_db(tmp_path: Path) -> AsyncIterator[RecoveryDatabase]:
    """Create an isolated SQLite database with one durable agent session."""
    database_path = tmp_path / "recovery.sqlite3"
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
            username="recovery-user",
            email="recovery@example.com",
            hashed_password="not-used",
            nickname="Recovery",
        )
        db.add(user)
        await db.flush()
        session = AgentSession(user_id=user.id, title="recovery", status="active")
        db.add(session)
        await db.commit()
        session_id = session.id

    try:
        yield RecoveryDatabase(session_factory=session_factory, session_id=session_id)
    finally:
        await engine.dispose()


async def _completed_runner(
    _db: AsyncSession,
    _receipt: ChildReceipt,
    _budget: Budget,
) -> ChildRunResult:
    return ChildRunResult(status="COMPLETED", result_summary="done")


async def _persist_row(
    recovery_db: RecoveryDatabase,
    *,
    child_id: str,
    status: str,
    parent_agent_id: str | None = None,
    agent_path: str | None = None,
    trace_id: str | None = None,
    status_changed_at: datetime | None = None,
) -> None:
    """Persist one registry row directly through CRUD transitions, bypassing SpawnManager.

    This mimics exactly what a previous process leaves behind: a durable row
    with no in-process task or held slot anywhere.
    """
    async with recovery_db.session_factory() as db:
        await agent_registry_crud.create(
            db,
            child_id=child_id,
            session_id=recovery_db.session_id,
            trace_id=trace_id or f"trace-{child_id}",
            role_version="t09-v1",
            parent_agent_id=parent_agent_id,
            agent_path=agent_path or f"/root/{child_id}",
            role="kb_explorer" if parent_agent_id is None else "reviewer",
            model="local-chat",
            tools_allowlist=["kb_read"],
            sandbox_mode="read-only",
            task_brief="遗留孤儿任务",
            budget={"max_steps": 5, "max_cost_usd": 0.5},
        )
        if status in {"SPAWNING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}:
            await agent_registry_crud.transition_status(db, child_id, "SPAWNING")
        if status in {"RUNNING", "COMPLETED", "FAILED", "CANCELLED"}:
            await agent_registry_crud.transition_status(db, child_id, "RUNNING")
        if status in {"COMPLETED", "FAILED"}:
            await agent_registry_crud.transition_status(db, child_id, status)
        if status == "CANCELLED":
            await agent_registry_crud.transition_status(db, child_id, "CANCELLED")
        await db.commit()

        if status_changed_at is not None:
            row = await agent_registry_crud.get(db, child_id)
            assert row is not None
            row.status_changed_at = status_changed_at
            await db.commit()


async def test_startup_reconciliation_closes_orphan_active_rows(
    recovery_db: RecoveryDatabase,
) -> None:
    await _persist_row(recovery_db, child_id="req-child", status="REQUESTED")
    await _persist_row(recovery_db, child_id="spawning-child", status="SPAWNING")
    await _persist_row(
        recovery_db,
        child_id="parent-child",
        status="RUNNING",
        trace_id="cascade-trace",
    )
    await _persist_row(
        recovery_db,
        child_id="nested-child",
        status="RUNNING",
        parent_agent_id="parent-child",
        agent_path="/root/parent-child/nested-child",
        trace_id="cascade-trace",
    )

    manager = SpawnManager(recovery_db.session_factory, child_runner=_completed_runner)
    closed = await manager.reconcile_startup()

    assert {receipt.child_id for receipt in closed} == {
        "req-child",
        "spawning-child",
        "parent-child",
        "nested-child",
    }
    assert all(receipt.status == "CLOSED" for receipt in closed)
    assert all(receipt.force_closed is False for receipt in closed)
    # Descendants close before their parent.
    order = [receipt.child_id for receipt in closed]
    assert order.index("nested-child") < order.index("parent-child")

    async with recovery_db.session_factory() as db:
        for child_id in ("req-child", "spawning-child"):
            row = await agent_registry_crud.get(db, child_id)
            assert row is not None
            assert row.status == "CLOSED"
        events = await agent_trace_event_crud.list_for_trace(db, "trace-req-child")
    assert [event.control for event in events] == ["CANCELLED", "CLOSED"]
    assert all(event.error_class == "infra" for event in events)


async def test_startup_reconciliation_closes_terminal_rows(
    recovery_db: RecoveryDatabase,
) -> None:
    await _persist_row(
        recovery_db,
        child_id="completed-orphan",
        status="COMPLETED",
        trace_id="terminal-trace",
    )

    manager = SpawnManager(recovery_db.session_factory, child_runner=_completed_runner)
    closed = await manager.reconcile_startup()

    assert [receipt.child_id for receipt in closed] == ["completed-orphan"]
    assert closed[0].status == "CLOSED"
    assert closed[0].force_closed is False

    async with recovery_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, "terminal-trace")
    assert [event.control for event in events] == ["CLOSED"]
    assert events[0].error_class == "infra"


async def test_reconciliation_does_not_release_unowned_semaphore_slots(
    recovery_db: RecoveryDatabase,
) -> None:
    await _persist_row(recovery_db, child_id="orphan-running", status="RUNNING")
    manager = SpawnManager(
        recovery_db.session_factory,
        child_runner=_completed_runner,
        max_concurrent_children=1,
    )

    # A BoundedSemaphore raises ValueError the moment release() is called more
    # times than it was ever acquired. This fresh manager's semaphore for this
    # session has never been acquired, so an erroneous `slots.release()` for
    # the unowned orphan below would blow up inside reconcile_startup itself.
    closed = await manager.reconcile_startup()
    assert closed[0].status == "CLOSED"

    first = await manager.spawn_agent(
        session_id=recovery_db.session_id,
        role="kb_explorer",
        task_brief="uses the only real slot",
    )
    with pytest.raises(SpawnRejectedError) as raised:
        await manager.spawn_agent(
            session_id=recovery_db.session_id,
            role="kb_explorer",
            task_brief="no slot left",
        )
    assert raised.value.reason == "max_concurrent_children"

    await manager.wait_agent(first.child_id)
    await manager.close_agent(first.child_id)


async def test_gc_closes_only_terminal_receipts_older_than_ttl(
    recovery_db: RecoveryDatabase,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _persist_row(
        recovery_db,
        child_id="old-completed",
        status="COMPLETED",
        status_changed_at=now - timedelta(seconds=120),
        trace_id="old-trace",
    )
    await _persist_row(
        recovery_db,
        child_id="fresh-completed",
        status="COMPLETED",
        status_changed_at=now - timedelta(seconds=10),
    )
    await _persist_row(
        recovery_db,
        child_id="still-running",
        status="RUNNING",
        status_changed_at=now - timedelta(seconds=120),
    )

    manager = SpawnManager(
        recovery_db.session_factory,
        child_runner=_completed_runner,
        terminal_receipt_ttl_seconds=60,
    )

    collected = await manager.collect_expired_receipts(now=now)

    assert [receipt.child_id for receipt in collected] == ["old-completed"]
    async with recovery_db.session_factory() as db:
        old_row = await agent_registry_crud.get(db, "old-completed")
        fresh_row = await agent_registry_crud.get(db, "fresh-completed")
        running_row = await agent_registry_crud.get(db, "still-running")
    assert old_row is not None and old_row.status == "CLOSED"
    assert fresh_row is not None and fresh_row.status == "COMPLETED"
    assert running_row is not None and running_row.status == "RUNNING"


async def test_gc_releases_a_locally_owned_terminal_slot(
    recovery_db: RecoveryDatabase,
) -> None:
    manager = SpawnManager(
        recovery_db.session_factory,
        child_runner=_completed_runner,
        max_concurrent_children=1,
        terminal_receipt_ttl_seconds=60,
    )
    child = await manager.spawn_agent(
        session_id=recovery_db.session_id,
        role="kb_explorer",
        task_brief="owned by this process",
    )
    completed = await manager.wait_agent(child.child_id)
    assert completed.status == "COMPLETED"
    runtime = manager._runtime(recovery_db.session_id)
    assert child.child_id in runtime.held_child_ids

    old_cutoff = datetime.now(UTC) - timedelta(seconds=120)
    async with recovery_db.session_factory() as db:
        row = await agent_registry_crud.get(db, child.child_id)
        assert row is not None
        row.status_changed_at = old_cutoff
        await db.commit()

    collected = await manager.collect_expired_receipts()

    assert [receipt.child_id for receipt in collected] == [child.child_id]
    assert child.child_id not in runtime.held_child_ids
    async with recovery_db.session_factory() as db:
        events = await agent_trace_event_crud.list_for_trace(db, child.trace_id)
    assert events[-1].control == "CLOSED"
    assert events[-1].error_class is None

    second = await manager.spawn_agent(
        session_id=recovery_db.session_id,
        role="kb_explorer",
        task_brief="slot was actually released",
    )
    await manager.wait_agent(second.child_id)
    await manager.close_agent(second.child_id)


async def test_shutdown_cancels_and_closes_all_local_children(
    recovery_db: RecoveryDatabase,
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
        return ChildRunResult(status="COMPLETED", result_summary="unreachable")

    manager = SpawnManager(
        recovery_db.session_factory,
        child_runner=gated_runner,
        max_concurrent_children=1,
    )
    child = await manager.spawn_agent(
        session_id=recovery_db.session_id,
        role="kb_explorer",
        task_brief="still running at shutdown",
    )
    await started.wait()

    await manager.shutdown()

    assert manager._tasks == {}
    runtime = manager._runtime(recovery_db.session_id)
    assert runtime.held_child_ids == set()
    async with recovery_db.session_factory() as db:
        row = await agent_registry_crud.get(db, child.child_id)
    assert row is not None
    assert row.status == "CLOSED"
    async with recovery_db.session_factory() as db:
        trace_count = len(await agent_trace_event_crud.list_for_trace(db, child.trace_id))

    # Calling shutdown() again must be a safe no-op.
    await manager.shutdown()

    assert manager._tasks == {}
    assert runtime.held_child_ids == set()
    async with recovery_db.session_factory() as db:
        repeated_trace_count = len(
            await agent_trace_event_crud.list_for_trace(db, child.trace_id)
        )
    assert repeated_trace_count == trace_count
    release.set()


async def test_startup_reconciles_executing_to_unknown(
    db_engine: AsyncEngine,
    test_user: User,
) -> None:
    """启动恢复应把遗留 EXECUTING 提案转为 UNKNOWN。"""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as db:
        session = await agent_session_crud.create(
            db,
            {"user_id": test_user.id, "title": "recovery", "status": "active"},
        )
        proposal = await hitl_proposal_crud.create(
            db,
            session_id=session.id,
            proposed_by_agent_id=None,
            action_type="notify",
            action_payload={"message": "test"},
        )
        await hitl_proposal_crud.decide(
            db, proposal.id, approve=True, reviewed_by_user_id=test_user.id
        )
        await hitl_proposal_crud.claim_execution(db, proposal.id)
        await db.commit()
        proposal_id = proposal.id

    changed = await reconcile_executing_proposals(session_factory)
    assert changed == 1
    async with session_factory() as db:
        persisted = await hitl_proposal_crud.get(db, proposal_id)
        assert persisted is not None
        assert persisted.status == "UNKNOWN"


async def test_recovery_is_idempotent(recovery_db: RecoveryDatabase) -> None:
    await _persist_row(
        recovery_db, child_id="orphan-a", status="RUNNING", trace_id="idempotent-trace"
    )
    manager = SpawnManager(recovery_db.session_factory, child_runner=_completed_runner)

    first = await manager.reconcile_startup()
    async with recovery_db.session_factory() as db:
        trace_count = len(await agent_trace_event_crud.list_for_trace(db, "idempotent-trace"))

    second = await manager.reconcile_startup()

    assert [receipt.child_id for receipt in first] == ["orphan-a"]
    assert second == ()
    async with recovery_db.session_factory() as db:
        repeated_trace_count = len(
            await agent_trace_event_crud.list_for_trace(db, "idempotent-trace")
        )
    assert repeated_trace_count == trace_count
