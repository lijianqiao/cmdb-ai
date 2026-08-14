"""FastAPI `lifespan` 的确定性顺序测试。

实现流程：
1. 用 monkeypatch 把 monitor sweep、CMDB diff、回执 GC 三个后台循环，以及
   `spawn_manager.reconcile_startup`/`shutdown`、`engine.dispose` 都换成
   只往一个共享 `events` 列表里追加事件的假实现，这样测试完全不碰真实数据库
   或网络连接，只验证 `lifespan()` 本身的编排顺序。
2. 进入并退出 `lifespan(app)`，断言：
   - 启动对账（reconcile）发生在 `yield`（也就是应用真正对外服务）之前；
   - 三个后台循环的取消发生在 `spawn_manager.shutdown()` 之前；
   - `spawn_manager.shutdown()` 发生在 `engine.dispose()` 之前。
3. 用一个包装过的 `asyncio.create_task` 记录 `lifespan` 内部实际创建的每个
   task，退出后断言它们全部 `done()`——证明后台循环的取消是被真正 await 过的，
   不会有 task 在应用关闭后仍然挂在事件循环里。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, cast

import pytest
from fastapi import FastAPI

import app.main as main_module
from app.agent.spawn import SpawnManager
from app.main import validate_single_worker_environment


@pytest.mark.parametrize("key", ["WEB_CONCURRENCY", "UVICORN_WORKERS"])
def test_rejects_multiple_workers(key: str) -> None:
    with pytest.raises(RuntimeError, match="只支持 1 个 Uvicorn worker"):
        validate_single_worker_environment({key: "2"})


def test_allows_default_and_one_worker() -> None:
    validate_single_worker_environment({})
    validate_single_worker_environment({"WEB_CONCURRENCY": "1"})
    validate_single_worker_environment({"UVICORN_WORKERS": "1"})


@pytest.mark.asyncio
async def test_lifespan_orders_reconcile_background_tasks_and_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    created_tasks: list[asyncio.Task[None]] = []
    monitor_started = asyncio.Event()
    diff_started = asyncio.Event()
    gc_started = asyncio.Event()
    real_create_task = asyncio.create_task

    def tracking_create_task(
        coro: Coroutine[Any, Any, None], *, name: str | None = None
    ) -> asyncio.Task[None]:
        task = real_create_task(coro, name=name)
        created_tasks.append(task)
        return task

    async def fake_reconcile() -> tuple[object, ...]:
        events.append("reconcile")
        return ()

    async def fake_hitl_reconcile(_session_factory: object) -> None:
        events.append("hitl-reconcile")

    async def fake_recover_turns(_db: object) -> None:
        events.append("recover-turns")

    def fake_validate_workers(_environment: object) -> None:
        events.append("validate-workers")

    async def fake_shutdown() -> None:
        events.append("spawn-shutdown")

    async def fake_monitor_loop() -> None:
        monitor_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("monitor-cancelled")
            raise

    async def fake_diff_loop() -> None:
        diff_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("diff-cancelled")
            raise

    async def fake_gc_loop(_manager: SpawnManager) -> None:
        gc_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("gc-cancelled")
            raise

    class _FakeEngine:
        async def dispose(self) -> None:
            events.append("engine-dispose")

    class _FakeDbSession:
        async def commit(self) -> None:
            events.append("recover-commit")

    class _FakeSessionContext:
        async def __aenter__(self) -> _FakeDbSession:
            return _FakeDbSession()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(asyncio, "create_task", tracking_create_task)
    monkeypatch.setattr(main_module, "run_monitor_sweep_loop", fake_monitor_loop)
    monkeypatch.setattr(main_module, "run_cmdb_diff_loop", fake_diff_loop)
    monkeypatch.setattr(main_module, "run_receipt_gc_loop", fake_gc_loop)
    monkeypatch.setattr(main_module, "validate_single_worker_environment", fake_validate_workers)
    monkeypatch.setattr(main_module, "reconcile_executing_proposals", fake_hitl_reconcile)
    monkeypatch.setattr(main_module, "AsyncSessionLocal", _FakeSessionContext)
    monkeypatch.setattr(main_module.agent_session_crud, "recover_active_turns", fake_recover_turns)
    monkeypatch.setattr(main_module.spawn_manager, "reconcile_startup", fake_reconcile)
    monkeypatch.setattr(main_module.spawn_manager, "shutdown", fake_shutdown)
    monkeypatch.setattr(main_module, "engine", _FakeEngine())

    async with main_module.lifespan(cast(FastAPI, None)):
        events.append("yielded")
        # Give the three background loops an actual first turn on the event
        # loop before the context manager cancels them below — cancelling a
        # task before it has ever run once means its own try/except never
        # gets a chance to execute at all.
        async with asyncio.timeout(1):
            await asyncio.gather(
                monitor_started.wait(), diff_started.wait(), gc_started.wait()
            )

    assert events.index("validate-workers") < events.index("hitl-reconcile")
    assert events.index("hitl-reconcile") < events.index("recover-turns")
    assert events.index("recover-turns") < events.index("reconcile")
    assert events.index("reconcile") < events.index("yielded")
    assert events.index("gc-cancelled") < events.index("spawn-shutdown")
    assert events.index("spawn-shutdown") < events.index("engine-dispose")
    assert {"monitor-cancelled", "diff-cancelled", "gc-cancelled"} <= set(events)

    assert len(created_tasks) == 3
    assert all(task.done() for task in created_tasks)
    assert all(task.cancelled() for task in created_tasks)
