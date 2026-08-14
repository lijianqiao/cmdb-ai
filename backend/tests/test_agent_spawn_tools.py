"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_spawn_tools.py
@DateTime: 2026-08-14
@Docs: Task 10 根 Agent Spawn 工具 schema 与 dispatcher 测试。
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agent.budget import Budget
from app.agent.spawn import (
    ChildBudgetSnapshot,
    ChildReceipt,
    ChildRunResult,
    SpawnManager,
    SpawnRejectedError,
)
from app.agent.spawn_tools import (
    SPAWN_TOOL_NAMES,
    build_spawn_tool_dispatcher,
    spawn_tool_schemas,
)
from app.agent.tool_dispatch import root_tool_schemas, tool_schemas_for
from app.models import Base
from app.models.agent_session import AgentSession
from app.models.user import User

_CHILD_ID_RE = re.compile(r"child_id[=:]\s*([^\s\n]+)")


def _make_receipt(
    *,
    child_id: str,
    session_id: int,
    role: str = "ops_explorer",
    task_brief: str = "检查资产 42",
    status: str = "RUNNING",
    result_summary: str | None = None,
) -> ChildReceipt:
    now = datetime.now(UTC)
    return ChildReceipt(
        child_id=child_id,
        trace_id="trace-safe",
        session_id=session_id,
        parent_agent_id=None,
        agent_path=f"/root/{child_id}",
        role=role,
        role_version="t09-v1",
        model="local-chat",
        tools_allowlist=("query_monitor_status",),
        sandbox_mode="read-only",
        task_brief=task_brief,
        budget=ChildBudgetSnapshot(
            max_steps=20,
            max_cost_usd=1.0,
            max_wall_time_seconds=120.0,
        ),
        status=status,
        result_summary=result_summary,
        artifacts=("secret-artifact-token",),
        created_at=now,
        status_changed_at=now,
        closed_at=now if status == "CLOSED" else None,
        force_closed=False,
    )


@dataclass
class FakeSpawnManager:
    """记录 spawn 参数并脚本化子 Agent 生命周期。"""

    spawn_kwargs: dict[str, Any] = field(default_factory=dict)
    _receipts: dict[str, ChildReceipt] = field(default_factory=dict)
    _session_receipts: dict[int, list[str]] = field(default_factory=dict)
    _next_index: int = 0
    wait_raises: Exception | None = None
    spawn_raises: Exception | None = None

    async def spawn_agent(
        self,
        *,
        session_id: int,
        role: str,
        task_brief: str,
        trace_id: str | None = None,
        parent_agent_id: str | None = None,
        model: str | None = None,
        tools_allowlist: object | None = None,
        budget: object | None = None,
        fork_mode: str = "none",
    ) -> ChildReceipt:
        self.spawn_kwargs = {
            "session_id": session_id,
            "role": role,
            "task_brief": task_brief,
            "trace_id": trace_id,
            "parent_agent_id": parent_agent_id,
            "model": model,
            "tools_allowlist": tools_allowlist,
            "budget": budget,
            "fork_mode": fork_mode,
        }
        if self.spawn_raises is not None:
            raise self.spawn_raises
        child_id = f"child-{self._next_index}"
        self._next_index += 1
        receipt = _make_receipt(
            child_id=child_id,
            session_id=session_id,
            role=role,
            task_brief=task_brief,
            status="SPAWNING",
        )
        self._receipts[child_id] = receipt
        self._session_receipts.setdefault(session_id, []).append(child_id)
        return receipt

    async def wait_agent(
        self,
        child_id: str,
        *,
        timeout_ms: int | None = None,
    ) -> ChildReceipt:
        if self.wait_raises is not None:
            raise self.wait_raises
        receipt = self._receipts[child_id]
        completed = _make_receipt(
            child_id=child_id,
            session_id=receipt.session_id,
            role=receipt.role,
            task_brief=receipt.task_brief,
            status="COMPLETED",
            result_summary="子任务完成摘要",
        )
        self._receipts[child_id] = completed
        return completed

    async def list_agents(self, session_id: int) -> tuple[ChildReceipt, ...]:
        ids = self._session_receipts.get(session_id, [])
        return tuple(self._receipts[child_id] for child_id in ids)

    async def close_agent(self, child_id: str) -> ChildReceipt:
        receipt = self._receipts[child_id]
        closed = _make_receipt(
            child_id=child_id,
            session_id=receipt.session_id,
            role=receipt.role,
            task_brief=receipt.task_brief,
            status="CLOSED",
            result_summary=receipt.result_summary,
        )
        self._receipts[child_id] = closed
        return closed


@pytest.fixture
def fake_spawn_manager() -> FakeSpawnManager:
    """可脚本化的 SpawnManager 替身。"""
    return FakeSpawnManager()


def test_spawn_schema_exposes_only_server_controlled_arguments() -> None:
  schemas = {item["function"]["name"]: item for item in spawn_tool_schemas()}
  parameters = schemas["spawn_agent"]["function"]["parameters"]
  assert set(parameters["properties"]) == {"role", "task_brief"}
  assert set(parameters["properties"]["role"]["enum"]) == {
      "classifier",
      "kb_explorer",
      "ops_explorer",
      "investigator",
      "reviewer",
  }
  assert "model" not in parameters["properties"]
  assert "tools_allowlist" not in parameters["properties"]
  assert "budget" not in parameters["properties"]


def test_spawn_tool_names_match_schemas() -> None:
    names = {item["function"]["name"] for item in spawn_tool_schemas()}
    assert names == set(SPAWN_TOOL_NAMES)


def test_root_and_child_schemas_exclude_spawn_tools() -> None:
    root_names = {item["function"]["name"] for item in root_tool_schemas()}
    child_names = {
        item["function"]["name"]
        for item in tool_schemas_for(
            ("kb_read", "query_cmdb", "query_monitor_status")
        )
    }
    for spawn_name in SPAWN_TOOL_NAMES:
        assert spawn_name not in root_names
        assert spawn_name not in child_names


async def test_root_spawn_dispatcher_spawns_waits_lists_and_closes(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=9)
    spawned = await dispatch(
        "spawn_agent",
        {"role": "ops_explorer", "task_brief": "检查资产 42 的当前监控状态"},
    )
    assert spawned.control == "ok"
    assert "child-0" in spawned.content

    waited = await dispatch("wait_agent", {"child_id": "child-0", "timeout_ms": 1000})
    assert "COMPLETED" in waited.content
    assert fake_spawn_manager.spawn_kwargs["fork_mode"] == "none"

    listed = await dispatch("list_agents", {})
    assert "child-0" in listed.content

    closed = await dispatch("close_agent", {"child_id": "child-0"})
    assert "CLOSED" in closed.content


async def test_spawn_dispatcher_rejects_unknown_role(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=1)
    result = await dispatch(
        "spawn_agent",
        {"role": "super_admin", "task_brief": "越权"},
    )
    assert result.control == "clarification"
    assert "参数无效" in result.content


async def test_spawn_dispatcher_rejects_out_of_bounds_timeout(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=1)
    await dispatch(
        "spawn_agent",
        {"role": "ops_explorer", "task_brief": "合法 brief"},
    )
    result = await dispatch("wait_agent", {"child_id": "child-0", "timeout_ms": 40000})
    assert result.control == "clarification"
    assert "参数无效" in result.content


async def test_spawn_dispatcher_rejects_other_session_child_id(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=1)
    await dispatch(
        "spawn_agent",
        {"role": "ops_explorer", "task_brief": "会话 9 的子任务"},
    )
    other_dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=99)
    result = await other_dispatch("wait_agent", {"child_id": "child-0", "timeout_ms": 1000})
    assert result.control == "failed"
    assert "ChildNotFoundError" in result.content


async def test_spawn_dispatcher_wait_timeout_does_not_cancel_child(
    tmp_path: Path,
) -> None:
    """wait 超时时 dispatcher 返回失败分类，子 Agent 仍在 RUNNING。"""
    database_path = tmp_path / "spawn-wait-timeout.sqlite3"
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
            username="spawn-wait",
            email="spawn-wait@example.com",
            hashed_password="not-used",
            nickname="SpawnWait",
        )
        db.add(user)
        await db.flush()
        session = AgentSession(user_id=user.id, title="wait-timeout", status="active")
        db.add(session)
        await db.commit()
        session_id = session.id

    async def slow_runner(
        _db: AsyncSession,
        _receipt: ChildReceipt,
        _budget: Budget,
    ) -> ChildRunResult:
        await asyncio.sleep(2)
        return ChildRunResult(status="COMPLETED", result_summary="慢任务")

    manager = SpawnManager(session_factory, child_runner=slow_runner)
    dispatch = build_spawn_tool_dispatcher(manager, session_id=session_id)
    spawned = await dispatch(
        "spawn_agent",
        {"role": "ops_explorer", "task_brief": "慢速监控检查"},
    )
    child_id = _CHILD_ID_RE.search(spawned.content).group(1)
    result = await dispatch("wait_agent", {"child_id": child_id, "timeout_ms": 50})
    assert result.control == "failed"
    assert "ChildWaitTimeoutError" in result.content
    receipts = await manager.list_agents(session_id)
    running = next(item for item in receipts if item.child_id == child_id)
    assert running.status == "RUNNING"
    await manager.close_agent(child_id)
    await engine.dispose()


async def test_spawn_dispatcher_hides_internal_exception_detail(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    fake_spawn_manager.spawn_raises = RuntimeError("secret spawn failure detail")
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=1)
    result = await dispatch(
        "spawn_agent",
        {"role": "ops_explorer", "task_brief": "触发内部异常"},
    )
    assert result.control == "failed"
    assert "RuntimeError" in result.content
    assert "secret spawn failure detail" not in result.content


async def test_spawn_dispatcher_maps_spawn_rejected_to_safe_message(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    fake_spawn_manager.spawn_raises = SpawnRejectedError("max_concurrent_children")
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=1)
    result = await dispatch(
        "spawn_agent",
        {"role": "ops_explorer", "task_brief": "并发已满"},
    )
    assert result.control == "rejected"
    assert "max_concurrent_children" in result.content
    assert "secret" not in result.content.lower()


async def test_safe_receipt_text_excludes_sensitive_fields(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=3)
    result = await dispatch(
        "spawn_agent",
        {"role": "ops_explorer", "task_brief": "只读监控取证"},
    )
    assert result.control == "ok"
    assert "tools_allowlist" not in result.content
    assert "budget" not in result.content
    assert "model" not in result.content
    assert "trace_id" not in result.content
    assert "secret-artifact-token" not in result.content
    assert "只读监控取证" in result.content
