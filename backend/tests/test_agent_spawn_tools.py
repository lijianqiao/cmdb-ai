"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_spawn_tools.py
@DateTime: 2026-08-14
@Docs: Task 10 根 Agent Spawn 工具 schema 与 dispatcher 测试。
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from app.agent.spawn import (
    ChildBudgetSnapshot,
    ChildReceipt,
    ChildRuntimeUnavailableError,
    SpawnRejectedError,
)
from app.agent.spawn_tools import (
    ORCHESTRATION_TOOL_NAMES,
    SPAWN_TOOL_NAMES,
    build_spawn_tool_dispatcher,
    spawn_tool_schemas,
)
from app.agent.tool_dispatch import root_tool_schemas, tool_schemas_for

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


def test_spawn_tool_names_match_schemas() -> None:
    names = {item["function"]["name"] for item in spawn_tool_schemas()}
    assert names == set(SPAWN_TOOL_NAMES)


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


_WORKFLOW_ARGS = {
    "incident_context": "三台设备同时离线",
    "branches": [
        {"name": "a", "objective": "查监控历史"},
        {"name": "b", "objective": "查 CMDB 拓扑"},
    ],
}


async def test_workflow_dispatcher_hides_internal_exception_detail(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    """工作流内部异常只回类型名，不透传原始文本。"""
    fake_spawn_manager.spawn_raises = RuntimeError("secret spawn failure detail")
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=1)

    result = await dispatch("investigate_root_cause", _WORKFLOW_ARGS)

    assert result.control == "failed"
    assert "RuntimeError" in result.content
    assert "secret spawn failure detail" not in result.content


async def test_workflow_dispatcher_maps_spawn_rejected_to_safe_message(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    """并发/配额拒绝原样透出限额名，让模型知道是「满了」而不是「坏了」。"""
    fake_spawn_manager.spawn_raises = SpawnRejectedError("max_concurrent_children")
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=1)

    result = await dispatch("investigate_root_cause", _WORKFLOW_ARGS)

    assert result.control == "rejected"
    assert "max_concurrent_children" in result.content
    assert "secret" not in result.content.lower()


async def test_workflow_dispatcher_passes_budget_exceeded_safe_reason(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    """子 Agent 超预算时错误分类固定在五类安全枚举内。"""
    fake_spawn_manager.spawn_raises = ChildRuntimeUnavailableError(
        "child-0",
        reason="budget_exceeded",
    )
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=1)

    result = await dispatch("investigate_root_cause", _WORKFLOW_ARGS)

    assert result.control == "failed"
    assert result.content == "工具 'investigate_root_cause' 执行失败: budget_exceeded"


def test_withdrawn_spawn_primitives_are_not_exposed_to_the_model() -> None:
    """五个 Spawn 原语已从模型工具面收起，不得回归。

    收起的理由见 spawn_tools 模块 docstring：原语要求模型正确完成
    spawn → wait → close 三步，漏 close 会占满并发槽；而
    investigate_root_cause 的自定义 branches 已经覆盖了并行取证的需求。
    这条断言是防止以后有人「顺手」把原语加回去。
    """
    exposed = {item["function"]["name"] for item in spawn_tool_schemas()}
    withdrawn = {"spawn_agent", "wait_agent", "send_input", "list_agents", "close_agent"}

    assert exposed.isdisjoint(withdrawn)
    assert exposed == {"classify_documents", "investigate_root_cause"}


async def test_withdrawn_primitives_are_rejected_by_dispatcher(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    """即使模型凭记忆硬调原语名，dispatcher 也必须拒绝而不是执行。"""
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=1)

    for name in ("spawn_agent", "wait_agent", "send_input", "list_agents", "close_agent"):
        result = await dispatch(name, {"role": "ops_explorer", "task_brief": "x"})
        assert result.control == "rejected", name
        assert name in result.content


def test_total_root_tool_count_stays_within_small_model_budget() -> None:
    """根 Agent 工具总数守门：19 → 14。

    本地小模型在工具数超过十来个之后选择准确率下降明显。这条断言不是要求
    永远 14 个，而是让「又加了一个工具」这件事必须被显式确认一次。
    """
    total = len(root_tool_schemas()) + len(spawn_tool_schemas())
    assert total == 14, f"根 Agent 工具数变成 {total}，如果是有意新增请同步更新本断言"
