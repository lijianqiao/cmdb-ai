"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_spawn_tools.py
@DateTime: 2026-08-14
@Docs: Task 10 根 Agent Spawn 工具 schema 与 dispatcher 测试。
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

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
    ORCHESTRATION_TOOL_NAMES,
    SPAWN_PRIMITIVE_TOOL_NAMES,
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
class _ChildScript:
    """按 spawn 顺序脚本化每个 child 的终态输出。"""

    result_summary: str | None = None
    status: Literal["COMPLETED", "FAILED"] = "COMPLETED"


@dataclass
class FakeSpawnManager:
    """记录 spawn 参数并脚本化子 Agent 生命周期。"""

    max_concurrent_children: int = 5
    scripts: list[_ChildScript] = field(default_factory=list)
    spawn_kwargs: dict[str, Any] = field(default_factory=dict)
    spawn_kwargs_history: list[dict[str, Any]] = field(default_factory=list)
    sent_inputs: list[tuple[str, str]] = field(default_factory=list)
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
        self.spawn_kwargs_history.append(dict(self.spawn_kwargs))
        if self.spawn_raises is not None:
            raise self.spawn_raises
        child_id = f"child-{self._next_index}"
        self._next_index += 1
        receipt = _make_receipt(
            child_id=child_id,
            session_id=session_id,
            role=role,
            task_brief=task_brief,
            status="RUNNING",
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
        index = int(child_id.removeprefix("child-"))
        script = (
            self.scripts[index]
            if index < len(self.scripts)
            else _ChildScript(result_summary="子任务完成摘要")
        )
        completed = _make_receipt(
            child_id=child_id,
            session_id=receipt.session_id,
            role=receipt.role,
            task_brief=receipt.task_brief,
            status=script.status,
            result_summary=script.result_summary,
        )
        self._receipts[child_id] = completed
        return completed

    async def list_agents(self, session_id: int) -> tuple[ChildReceipt, ...]:
        ids = self._session_receipts.get(session_id, [])
        return tuple(self._receipts[child_id] for child_id in ids)

    async def send_input(self, child_id: str, message: str) -> ChildReceipt:
        receipt = self._receipts[child_id]
        if receipt.status != "RUNNING":
            raise SpawnRejectedError("child_not_running")
        self.sent_inputs.append((child_id, message))
        return receipt

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


def test_spawn_primitives_and_orchestration_tools_are_exposed_separately() -> None:
    """五原语与两编排工作流应分别登记并合并为七工具全集。"""
    assert SPAWN_PRIMITIVE_TOOL_NAMES == {
        "spawn_agent",
        "wait_agent",
        "send_input",
        "list_agents",
        "close_agent",
    }
    assert ORCHESTRATION_TOOL_NAMES == {
        "classify_documents",
        "investigate_root_cause",
    }
    assert SPAWN_TOOL_NAMES == SPAWN_PRIMITIVE_TOOL_NAMES | ORCHESTRATION_TOOL_NAMES
    assert {item["function"]["name"] for item in spawn_tool_schemas()} == SPAWN_TOOL_NAMES


def test_orchestration_tool_schemas_use_strict_workflow_arguments() -> None:
    """编排工具 schema 不得暴露 session_id、模型或预算等服务端字段。"""
    schemas = {item["function"]["name"]: item for item in spawn_tool_schemas()}
    classify_params = schemas["classify_documents"]["function"]["parameters"]
    root_cause_params = schemas["investigate_root_cause"]["function"]["parameters"]
    assert classify_params["properties"]["documents"]["minItems"] == 2
    assert root_cause_params["properties"]["incident_context"]["minLength"] == 1
    forbidden = {"session_id", "model", "budget", "tools_allowlist"}
    for tool_name in ORCHESTRATION_TOOL_NAMES:
        parameters = schemas[tool_name]["function"]["parameters"]
        assert forbidden.isdisjoint(parameters.get("properties", {}))


def _classification_json(
    document_id: int,
    *,
    confidence: float = 0.95,
    needs_review: bool = False,
    category: str = "网络",
) -> str:
    return (
        f'{{"document_id":{document_id},"recommended_category":"{category}",'
        f'"confidence":{confidence},"needs_review":{str(needs_review).lower()},"reason":"证据充分"}}'
    )


def _finding_json(branch: str) -> str:
    return (
        f'{{"branch":"{branch}","hypothesis":"网络抖动","confidence":0.6,'
        '"evidence":["探测记录"],"gaps":["缺少变更日志"],"next_checks":["复查拓扑"]}'
    )


def _synthesis_json() -> str:
    return (
        '{"summary":"综合结论","likely_causes":["网络抖动"],'
        '"evidence_gaps":["变更日志"],"recommended_next_steps":["观察"]}'
    )


async def test_spawn_dispatcher_runs_classify_documents_workflow(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    """classify_documents 应走真实编排并返回 JSON outcome。"""
    fake_spawn_manager.scripts = [
        _ChildScript(result_summary=_classification_json(1, category="网络")),
        _ChildScript(result_summary=_classification_json(2, category="数据库", confidence=0.9)),
    ]
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=9)
    result = await dispatch(
        "classify_documents",
        {
            "documents": [
                {"document_id": 1, "title": "交换机", "file_path": "network/a.md"},
                {"document_id": 2, "title": "数据库", "file_path": "db/b.md"},
            ],
            "allowed_categories": ["网络", "数据库"],
        },
    )
    payload = json.loads(result.content)
    assert result.control == "ok"
    assert [item["document_id"] for item in payload["suggestions"]] == [1, 2]
    assert fake_spawn_manager.spawn_kwargs_history[0]["session_id"] == 9


async def test_spawn_dispatcher_runs_investigate_root_cause_workflow(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    """investigate_root_cause 应走真实编排并返回 findings 与 synthesis。"""
    fake_spawn_manager.scripts = [
        _ChildScript(result_summary=_finding_json("branch-a")),
        _ChildScript(result_summary=_finding_json("branch-b")),
        _ChildScript(result_summary=_synthesis_json()),
    ]
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=3)
    result = await dispatch(
        "investigate_root_cause",
        {
            "incident_context": "核心交换机抖动",
            "branches": [
                {"name": "branch-a", "objective": "核查监控历史"},
                {"name": "branch-b", "objective": "核查拓扑依赖"},
            ],
        },
    )
    payload = json.loads(result.content)
    assert result.control == "ok"
    assert len(payload["findings"]) == 2
    assert payload["review"] is not None
    assert payload["review"]["summary"] == "综合结论"


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


def test_send_input_schema_exposes_child_and_message_only() -> None:
    schemas = {item["function"]["name"]: item for item in spawn_tool_schemas()}
    properties = schemas["send_input"]["function"]["parameters"]["properties"]
    assert set(properties) == {"child_id", "message"}


async def test_spawn_dispatcher_sends_input_to_current_session_child(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=9)
    await dispatch("spawn_agent", {"role": "ops_explorer", "task_brief": "检查资产"})
    result = await dispatch(
        "send_input", {"child_id": "child-0", "message": "再核查最近五分钟"}
    )
    assert result.control == "ok"
    assert fake_spawn_manager.sent_inputs == [("child-0", "再核查最近五分钟")]


async def test_spawn_dispatcher_rejects_other_session_send_input(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=1)
    await dispatch(
        "spawn_agent",
        {"role": "ops_explorer", "task_brief": "本会话子任务"},
    )
    other_dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=99)
    result = await other_dispatch(
        "send_input",
        {"child_id": "child-0", "message": "跨会话补充输入"},
    )
    assert result.control == "failed"
    assert "ChildNotFoundError" in result.content
    assert fake_spawn_manager.sent_inputs == []


async def test_spawn_dispatcher_rejects_blank_send_input_message(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=1)
    await dispatch(
        "spawn_agent",
        {"role": "ops_explorer", "task_brief": "合法 brief"},
    )
    result = await dispatch("send_input", {"child_id": "child-0", "message": ""})
    assert result.control == "clarification"
    assert "参数无效" in result.content


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
