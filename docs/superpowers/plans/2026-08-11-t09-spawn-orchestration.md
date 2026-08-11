# T09 · Spawn 编排 + 角色目录 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the in-process dynamic child-Agent runtime promised by T09: durable `ChildReceipt` records, isolated child transcripts, a versioned read-only role/tool catalog, the five spawn primitives, and two leak-free parallel workflows for batch document classification and multi-source root-cause investigation.

**Architecture:** `SpawnManager` in `app/agent/spawn.py` is the only owner of child `asyncio.Task` objects and child lifecycle transactions; `AgentRegistry` remains the durable source of truth, while a per-session `asyncio.BoundedSemaphore` enforces the five-child active-slot limit. Every child shares the root `AgentSession` for ownership/audit but stores messages under its own nullable `AgentMessage.agent_id`, so `fork_mode="none"` is real rather than a prompt convention. Role definitions and JSON tool schemas are code-owned, children receive only read-only T07/T08 tools, and the two workflows compose `spawn → wait → close` in bounded waves so `COMPLETED` receipts never leak slots.

**Tech Stack:** Python 3.14.3, FastAPI, SQLAlchemy 2 async, PostgreSQL + Alembic, Pydantic 2, in-process `asyncio`, pytest + pytest-asyncio, uv. No new dependency is required.

## Global Constraints

- Python `>=3.14,<3.15` only. Run every Python/test command from `backend/` with `uv run`; never invoke a bare system `python`, `pytest`, `mypy`, or `ruff`.
- Work directly on `master`; do not create a branch or PR. Each task ends in one focused Chinese commit with a title, a blank line, explanatory bullets, and no `Co-Authored-By` line. Do not push unless the project owner separately asks.
- TDD order is mandatory: add the named failing test, observe the stated failure, add only the production code needed for that task, rerun the focused tests, then commit.
- No real LLM request and no Docker PostgreSQL integration test is required for T09. Model behavior is injected with fake `ChatFn`/`ChildRunner`; SQLite exercises persistence and orchestration. `uv run alembic heads` verifies a single migration head without connecting to a database.
- Do not add a dependency. Pydantic, SQLAlchemy, pytest, and pytest-asyncio already exist in `backend/pyproject.toml`.
- Child execution is deliberately **single-process**. Run the Agent executor with one application worker; a second worker cannot see the first worker's in-memory `asyncio.Task` objects. Cross-process queues/Redis are outside T09.
- CRUD methods flush only. `SpawnManager`, workflow entry points, startup reconciliation, and GC are top-level orchestration owners, so they open independent `AsyncSessionLocal` sessions and commit/rollback their own lifecycle transactions. Never share the root request's `AsyncSession` across concurrent children.
- `fork_mode` accepts only the literal string `"none"`. A child receives its versioned role system prompt plus its explicit `task_brief`; it never receives root history or a sibling transcript.
- Root transcript rows keep `AgentMessage.agent_id = NULL`; child rows store `agent_id = child_id`. Existing root callers remain source-compatible because every new `agent_id` argument defaults to `None`.
- Every role is read-only. `classifier` and `kb_explorer` can use knowledge tools; `ops_explorer` can use CMDB/monitor tools; `investigator` and `reviewer` can use both read-only groups. No shell, arbitrary SQL, filesystem-write, HITL, or device-control tool enters a child allowlist.
- Model overrides must name a `MODELS` entry whose capability is `"chat"`; `local-embedding` can never be selected as a child chat model. Role model tiers are descriptive (`fast`/`balanced`/`reasoning`) while all five roles default to the currently registered `local-chat` key.
- Same-session active children, including `COMPLETED`/`FAILED`/`CANCELLED` receipts, hold their semaphore slots until `close_agent`. `close_agent` is idempotent and closes descendants depth-first.
- Same-session active-child limit defaults to 5; maximum nesting depth defaults to 2; only `reviewer` may be spawned below another child. Session child count and reserved child-dollar budgets are cumulative safety limits.
- Structured classifier/investigator/reviewer output is parsed strictly with Pydantic. Invalid JSON is an explicit failed/needs-review result; never silently treat a parse failure as success.
- Batch classification is advisory: T09 returns proposed categories but does not update `KnowledgeDocument.category_id`. Root-cause investigation is also read-only and never creates remediation proposals; proposal/HITL execution belongs to T10.
- The architecture's example “CMDB recent changes” branch cannot query `audit_logs` through the current T08 tool contract. T09's built-in root-cause branches therefore use monitoring history, CMDB topology/ownership, and peer-scope status; a task brief must state the missing change-log evidence instead of bypassing CRUD or inventing a hidden SQL path.
- The T07/T08 callable signatures are the executable source of truth for T09 adapters (`category_id`, `ip_prefix`, and `since_limit`). Do not rename already-delivered tool parameters merely to match older architecture examples.
- A tool result with `control="failed"` is returned to the child model so it may correct its approach within the remaining budget. A rejected/clarification/approval control still ends the loop; an uncaught dispatcher/runtime exception ends the child as `FAILED`.
- A model response that crosses its dollar limit is still charged. Preserve an already-produced final answer as `COMPLETED`; if the over-budget response contains tool calls, do not execute them and end the child as `FAILED/policy_reject`.
- Full validation at the end: `uv run pytest -v`, `uv run mypy app`, and `uv run ruff check .` from `backend/`.

## File Map

| File | Responsibility |
| :--- | :--- |
| `backend/app/models/agent_message.py` + migration | Add nullable child identity to the shared session transcript without changing root rows. |
| `backend/app/crud/agent_message.py`, `backend/app/agent/session.py`, `backend/app/agent/loop.py` | Read/write one exact Agent transcript and inject code-owned role instructions. |
| `backend/app/core/llm.py`, `backend/app/agent/budget.py` | Distinguish chat/embedding models and charge configured token prices into a child budget. |
| `backend/app/agent/roles.py` | Versioned five-role catalog, default model tier, instructions, and immutable tool allowlists. |
| `backend/app/agent/tool_dispatch.py` | Exact JSON schemas, Pydantic argument validation, allowlist enforcement, and dispatch to T07/T08 read tools. |
| `backend/app/crud/agent_registry.py` | Durable ChildReceipt listing/tree queries used by limits, close, reconciliation, and GC. |
| `backend/app/agent/spawn.py` | ChildReceipt DTO, limits, spawn/run/wait/send/list/close primitives, lifecycle tracing, reconciliation, and slot GC. |
| `backend/app/agent/orchestration.py` | Bounded parallel-wave helper plus batch-classification and root-cause workflows. |
| `backend/app/main.py` | Reconcile orphaned rows at startup; run receipt GC; cancel children cleanly at shutdown. |
| `backend/tests/test_agent_spawn_integration.py` | Cross-component invariant test proving transcript isolation, strict workflow output, and zero active-slot leaks without real external services. |

## Explicitly Out of Scope

- HTTP/WebSocket endpoints and a root-chat `ToolDispatcher` adapter. T09 exposes typed Python primitives; T11 supplies the user-facing transport and root loop integration.
- Applying classification proposals to the database, changing knowledge file locations, or writing CMDB records. Those are write paths and need an explicit product/API contract.
- A new `query_audit_logs` or `query_cmdb_changes` tool. The current role must report that evidence gap; it must not call `audit_logs` directly.
- Multi-process/cross-host task recovery, Redis, a queue, distributed semaphore, and resumable child model execution.
- Session compaction reconciliation beyond the durable registry and per-child transcript key. The architecture maps full compact/reconcile productization to P5.

---

### Task 1: Add the versioned five-role catalog

**Files:**
- Create: `backend/app/agent/roles.py`
- Create: `backend/tests/test_agent_roles.py`

**Interfaces:**
- Consumes: the registered `local-chat` model key and the seven read-only tools delivered by T07/T08.
- Produces:
  - `RoleName`, `ToolName`, and `ModelTier` literal aliases
  - immutable `RoleDefinition(name, version, description, instructions, model_key, model_tier, sandbox_mode, tools_allowlist)`
  - immutable `ROLE_CATALOG`
  - `get_role(name: str) -> RoleDefinition`
  - `list_roles() -> tuple[RoleDefinition, ...]`

- [ ] **Step 1: Write catalog tests before the module exists**

Create `backend/tests/test_agent_roles.py`:

```python
"""Contract tests for the code-owned child-Agent role catalog."""

import pytest

from app.agent.roles import ROLE_CATALOG, UnknownAgentRoleError, get_role, list_roles


def test_catalog_contains_all_architecture_roles() -> None:
    assert tuple(ROLE_CATALOG) == (
        "classifier",
        "kb_explorer",
        "ops_explorer",
        "investigator",
        "reviewer",
    )


def test_every_role_is_versioned_described_and_read_only() -> None:
    for role in list_roles():
        assert role.version == "t09-v1"
        assert len(role.description) >= 20
        assert len(role.instructions) >= 80
        assert role.model_key == "local-chat"
        assert role.sandbox_mode == "read-only"


def test_role_tool_boundaries_are_least_privilege() -> None:
    knowledge = {"kb_glob", "kb_grep", "kb_read", "kb_semantic_search"}
    ops = {"query_cmdb", "query_cmdb_dependencies", "query_monitor_status"}

    assert set(get_role("classifier").tools_allowlist) == {
        "kb_glob",
        "kb_grep",
        "kb_read",
    }
    assert set(get_role("kb_explorer").tools_allowlist) == knowledge
    assert set(get_role("ops_explorer").tools_allowlist) == ops
    assert set(get_role("investigator").tools_allowlist) == knowledge | ops
    assert set(get_role("reviewer").tools_allowlist) == knowledge | ops


def test_role_model_tiers_match_architecture() -> None:
    assert get_role("classifier").model_tier == "fast"
    assert get_role("kb_explorer").model_tier == "fast"
    assert get_role("ops_explorer").model_tier == "fast"
    assert get_role("investigator").model_tier == "balanced"
    assert get_role("reviewer").model_tier == "reasoning"


def test_unknown_role_fails_closed() -> None:
    with pytest.raises(UnknownAgentRoleError, match="unknown child-Agent role"):
        get_role("worker")


def test_catalog_and_role_definitions_are_immutable() -> None:
    with pytest.raises(TypeError):
        ROLE_CATALOG["classifier"] = get_role("reviewer")  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        get_role("classifier").model_key = "other"  # type: ignore[misc]
```

Import `FrozenInstanceError` from `dataclasses`. The immutable catalog plus the Task 5 persisted `role_version` make historical receipts explainable; do not duplicate entire prompts into registry JSON.

- [ ] **Step 2: Run the new file and verify import failure**

Run: `uv run pytest tests/test_agent_roles.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'app.agent.roles'`.

- [ ] **Step 3: Implement the complete role catalog**

Create `backend/app/agent/roles.py`:

```python
"""Versioned, code-owned child-Agent role catalog for T09.

Descriptions tell the root orchestrator when delegation is appropriate;
instructions constrain how the selected child works. Tool permissions are
separate immutable allowlists and are rechecked by tool_dispatch.py, so a
prompt injection cannot expand a child's capabilities.
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping, cast

type RoleName = Literal[
    "classifier",
    "kb_explorer",
    "ops_explorer",
    "investigator",
    "reviewer",
]
type ToolName = Literal[
    "kb_glob",
    "kb_grep",
    "kb_read",
    "kb_semantic_search",
    "query_cmdb",
    "query_cmdb_dependencies",
    "query_monitor_status",
]
type ModelTier = Literal["fast", "balanced", "reasoning"]

ROLE_CATALOG_VERSION = "t09-v1"

_KNOWLEDGE_TOOLS: tuple[ToolName, ...] = (
    "kb_glob",
    "kb_grep",
    "kb_read",
    "kb_semantic_search",
)
_OPS_TOOLS: tuple[ToolName, ...] = (
    "query_cmdb",
    "query_cmdb_dependencies",
    "query_monitor_status",
)


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    """One immutable role contract selected by `spawn_agent`."""

    name: RoleName
    version: str
    description: str
    instructions: str
    model_key: str
    model_tier: ModelTier
    sandbox_mode: Literal["read-only"]
    tools_allowlist: tuple[ToolName, ...]


class UnknownAgentRoleError(ValueError):
    """Raised before spawning when a role is not in the hosted catalog."""


_ROLE_CATALOG: dict[RoleName, RoleDefinition] = {
    "classifier": RoleDefinition(
        name="classifier",
        version=ROLE_CATALOG_VERSION,
        description=(
            "仅用于两份及以上知识文档的并行归类建议；单文档分类应由根 Agent "
            "直接完成，不创建子 Agent。"
        ),
        instructions="""你是运维知识文档分类器，只读取 task_brief 指定的文档。
先用 kb_read 获取正文；只有路径或关键词不明确时才使用 kb_glob/kb_grep。
不得修改文件、数据库或分类。只能从 allowed_categories 选择；若均不合适，
把 recommended_category 写为建议的新 code，并把 needs_review 设为 true。
最终回答必须是一个 JSON 对象，不要 Markdown 代码围栏：
{"document_id":整数,"recommended_category":"code","confidence":0到1,
"needs_review":布尔值,"reason":"有正文证据的简短理由"}。""",
        model_key="local-chat",
        model_tier="fast",
        sandbox_mode="read-only",
        tools_allowlist=("kb_glob", "kb_grep", "kb_read"),
    ),
    "kb_explorer": RoleDefinition(
        name="kb_explorer",
        version=ROLE_CATALOG_VERSION,
        description=(
            "用于需要独立知识检索上下文的只读取证，例如跨多份 SOP 查证；单次 "
            "Glob/Grep/Read 查询不要委派。"
        ),
        instructions="""你是运维知识库检索员。只处理 task_brief 明确的问题，
优先 Glob/Grep/Read 获取可引用原文，关键词检索不足时才用 semantic search。
不得修改知识文件或数据库。结论必须区分“文档明确说明”“根据证据推断”与
“当前资料没有覆盖”，并在摘要中写出文件路径或 document_id。""",
        model_key="local-chat",
        model_tier="fast",
        sandbox_mode="read-only",
        tools_allowlist=_KNOWLEDGE_TOOLS,
    ),
    "ops_explorer": RoleDefinition(
        name="ops_explorer",
        version=ROLE_CATALOG_VERSION,
        description=(
            "用于一个独立 CMDB 或监控数据源的结构化取证；单设备在线状态查询 "
            "应由根 Agent 直接调用工具。"
        ),
        instructions="""你是运维结构化数据检索员。只调用 CMDB、依赖图和监控
状态只读工具，围绕 task_brief 返回资产身份、归属、拓扑或状态证据。
“尚未探测”不等于“离线”；监控当前状态必须以最新事件派生结果为准。
不得修改资产、目标或状态记录，证据不足时明确列出缺少的筛选条件。""",
        model_key="local-chat",
        model_tier="fast",
        sandbox_mode="read-only",
        tools_allowlist=_OPS_TOOLS,
    ),
    "investigator": RoleDefinition(
        name="investigator",
        version=ROLE_CATALOG_VERSION,
        description=(
            "用于根因排查中的一个独立假设分支，可跨知识、CMDB 和监控只读取证；"
            "同一事故的多个分支适合并行。"
        ),
        instructions="""你是根因调查员，只验证 task_brief 指定的一个假设分支，
不要提前综合兄弟分支。允许跨知识、CMDB、依赖和监控工具取只读证据。
不得把时间相关性表述为因果，也不得伪造当前工具没有提供的 CMDB 变更日志。
最终回答必须是一个 JSON 对象，不要 Markdown 代码围栏：
{"branch":"分支名","hypothesis":"被检验的假设","confidence":0到1,
"evidence":["证据"],"gaps":["证据缺口"],"next_checks":["下一步只读检查"]}。""",
        model_key="local-chat",
        model_tier="balanced",
        sandbox_mode="read-only",
        tools_allowlist=_KNOWLEDGE_TOOLS + _OPS_TOOLS,
    ),
    "reviewer": RoleDefinition(
        name="reviewer",
        version=ROLE_CATALOG_VERSION,
        description=(
            "只在分类冲突、新分类建议、调查分支矛盾或需要最终证据复核时使用；"
            "它不执行写入，也不替代根 Agent 面向用户。"
        ),
        instructions="""你是只读复核员。task_brief 会说明 workflow 和精确输出
契约；检查每条结论是否有工具证据、不同分支是否冲突、是否把未知写成已知。
必要时可调用只读知识/CMDB/监控工具复核，但不得修改数据或发起处置。
严格按 task_brief 的 output_contract 返回一个 JSON 对象，不要 Markdown 围栏，
并把无法验证、缺少变更日志或解析失败的内容列入 evidence_gaps。""",
        model_key="local-chat",
        model_tier="reasoning",
        sandbox_mode="read-only",
        tools_allowlist=_KNOWLEDGE_TOOLS + _OPS_TOOLS,
    ),
}

ROLE_CATALOG: Mapping[RoleName, RoleDefinition] = MappingProxyType(_ROLE_CATALOG)


def get_role(name: str) -> RoleDefinition:
    """Return one hosted role or fail closed before allocating any resources."""
    if name not in ROLE_CATALOG:
        raise UnknownAgentRoleError(f"unknown child-Agent role {name!r}")
    return ROLE_CATALOG[cast(RoleName, name)]


def list_roles() -> tuple[RoleDefinition, ...]:
    """Return role definitions in stable catalog order."""
    return tuple(ROLE_CATALOG.values())
```

- [ ] **Step 4: Run tests and static checks**

Run:

```bash
uv run pytest tests/test_agent_roles.py -v
uv run mypy app/agent/roles.py
uv run ruff check app/agent/roles.py tests/test_agent_roles.py
```

Expected: all pass/clean.

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent/roles.py backend/tests/test_agent_roles.py
git commit -m "新增 T09 子 Agent 角色目录

- 补齐 classifier、kb_explorer、ops_explorer、investigator、reviewer 五个架构角色
- 为角色说明、system instructions 和工具白名单统一加 t09-v1 版本，便于 trace 与 Eval 对比
- 所有子角色坚持 read-only 最小权限，分类和调查角色约束为严格 JSON 输出"
```

---

### Task 2: Build the schema-validated read-only tool dispatcher

**Files:**
- Create: `backend/app/agent/tool_dispatch.py`
- Create: `backend/tests/test_agent_tool_dispatch.py`

**Interfaces:**
- Consumes: `ToolResult`/`ToolDispatcher`, T07 knowledge tool functions, T08 ops tool functions, and Task 1 `ToolName`.
- Produces:
  - `TOOL_SCHEMA_VERSION = "t09-v1"`
  - `tool_schemas_for(allowlist: Iterable[str]) -> list[dict[str, Any]]`
  - `build_tool_dispatcher(db, allowlist) -> ToolDispatcher`
  - fail-closed controls: unauthorized/unknown → `rejected`, invalid arguments → `clarification`, caught execution exception → `failed`.

- [ ] **Step 1: Write dispatcher contract tests**

Create `backend/tests/test_agent_tool_dispatch.py`:

```python
"""Allowlist, JSON-schema, and argument-validation tests for child tools."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import tool_dispatch
from app.agent.loop import ToolResult
from app.agent.tool_dispatch import build_tool_dispatcher, tool_schemas_for

pytestmark = pytest.mark.asyncio


def test_tool_schemas_expose_only_requested_names() -> None:
    schemas = tool_schemas_for(("kb_read", "query_monitor_status"))

    assert [schema["function"]["name"] for schema in schemas] == [
        "kb_read",
        "query_monitor_status",
    ]
    for schema in schemas:
        assert schema["type"] == "function"
        assert schema["function"]["parameters"]["additionalProperties"] is False


def test_all_seven_registered_tools_have_strict_schemas() -> None:
    names = (
        "kb_glob",
        "kb_grep",
        "kb_read",
        "kb_semantic_search",
        "query_cmdb",
        "query_cmdb_dependencies",
        "query_monitor_status",
    )

    schemas = tool_schemas_for(names)

    assert [item["function"]["name"] for item in schemas] == list(names)
    assert all(
        item["function"]["parameters"]["additionalProperties"] is False
        for item in schemas
    )


async def test_dispatch_rejects_tool_outside_allowlist(db_session: AsyncSession) -> None:
    dispatch = build_tool_dispatcher(db_session, ("kb_read",))

    result = await dispatch("query_cmdb", {})

    assert result.control == "rejected"
    assert "不在角色白名单" in result.content


async def test_dispatch_rejects_unknown_tool_even_if_persisted_allowlist_contains_it(
    db_session: AsyncSession,
) -> None:
    dispatch = build_tool_dispatcher(db_session, ("unknown_tool",))

    result = await dispatch("unknown_tool", {})

    assert result.control == "rejected"
    assert "未知工具" in result.content


async def test_dispatch_requests_clarification_for_invalid_arguments(
    db_session: AsyncSession,
) -> None:
    dispatch = build_tool_dispatcher(db_session, ("kb_read",))

    result = await dispatch("kb_read", {"path": "sop.md", "offset": -1})

    assert result.control == "clarification"
    assert "参数无效" in result.content


async def test_dispatch_does_not_coerce_argument_types(db_session: AsyncSession) -> None:
    dispatch = build_tool_dispatcher(db_session, ("kb_read",))

    result = await dispatch("kb_read", {"path": "sop.md", "offset": "1"})

    assert result.control == "clarification"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("kb_glob", {"pattern": ""}),
        ("kb_grep", {"pattern": "x", "context_lines": 21}),
        ("kb_read", {"path": "x", "unexpected": True}),
        ("kb_semantic_search", {"query": "x", "top_k": 21}),
        ("query_cmdb", {"ip": "10.0.0.1", "business_system": "ops"}),
        ("query_cmdb_dependencies", {"asset_id": 1, "max_depth": 6}),
        ("query_monitor_status", {"target_ids": [1], "ip_prefix": "10."}),
    ],
)
async def test_every_tool_rejects_its_boundary_violation(
    db_session: AsyncSession, tool_name: str, arguments: dict[str, Any]
) -> None:
    dispatch = build_tool_dispatcher(db_session, (tool_name,))

    result = await dispatch(tool_name, arguments)

    assert result.control == "clarification"


async def test_dispatch_calls_validated_knowledge_tool(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_kb_read(path: str, *, offset: int, limit: int | None) -> ToolResult:
        captured.update(path=path, offset=offset, limit=limit)
        return ToolResult(control="ok", content="document")

    monkeypatch.setattr(tool_dispatch, "kb_read", fake_kb_read)
    dispatch = build_tool_dispatcher(db_session, ("kb_read",))

    result = await dispatch("kb_read", {"path": "sop/a.md", "limit": 100})

    assert result == ToolResult(control="ok", content="document")
    assert captured == {"path": "sop/a.md", "offset": 0, "limit": 100}


async def test_dispatch_calls_validated_db_tool(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_query_cmdb(
        db: AsyncSession,
        *,
        asset_ids: list[int] | None,
        ip: str | None,
        business_system: str | None,
    ) -> ToolResult:
        assert db is db_session
        captured.update(
            asset_ids=asset_ids,
            ip=ip,
            business_system=business_system,
        )
        return ToolResult(control="ok", content="asset")

    monkeypatch.setattr(tool_dispatch, "query_cmdb", fake_query_cmdb)
    dispatch = build_tool_dispatcher(db_session, ("query_cmdb",))

    result = await dispatch("query_cmdb", {"ip": "10.0.0.5"})

    assert result.control == "ok"
    assert captured == {
        "asset_ids": None,
        "ip": "10.0.0.5",
        "business_system": None,
    }


async def test_dispatch_hides_internal_exception_detail(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_kb_read(path: str, *, offset: int, limit: int | None) -> ToolResult:
        raise RuntimeError("secret database address")

    monkeypatch.setattr(tool_dispatch, "kb_read", broken_kb_read)
    dispatch = build_tool_dispatcher(db_session, ("kb_read",))

    result = await dispatch("kb_read", {"path": "sop/a.md"})

    assert result.control == "failed"
    assert "RuntimeError" in result.content
    assert "secret database address" not in result.content
```

- [ ] **Step 2: Run the test file and verify import failure**

Run: `uv run pytest tests/test_agent_tool_dispatch.py -v`

Expected: FAIL during collection because `app.agent.tool_dispatch` does not exist.

- [ ] **Step 3: Implement exact argument models, schemas, and dispatch**

Create `backend/app/agent/tool_dispatch.py`:

```python
"""Validated dispatcher for the seven T07/T08 read-only Agent tools."""

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.knowledge_tools import kb_glob, kb_grep, kb_read, kb_semantic_search
from app.agent.loop import ToolDispatcher, ToolResult
from app.agent.ops_tools import query_cmdb, query_cmdb_dependencies, query_monitor_status
from app.agent.roles import ToolName

TOOL_SCHEMA_VERSION = "t09-v1"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class KbGlobArgs(_Args):
    pattern: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=100)


class KbGrepArgs(_Args):
    pattern: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=100)
    context_lines: int = Field(default=0, ge=0, le=20)


class KbReadArgs(_Args):
    path: str = Field(min_length=1, max_length=500)
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=4000, ge=1, le=32_000)


class KbSemanticSearchArgs(_Args):
    query: str = Field(min_length=1, max_length=2000)
    category_id: int | None = Field(default=None, ge=1)
    top_k: int = Field(default=5, ge=1, le=20)


class QueryCmdbArgs(_Args):
    asset_ids: list[int] | None = Field(default=None, max_length=100)
    ip: str | None = Field(default=None, min_length=1, max_length=45)
    business_system: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def at_most_one_filter(self) -> "QueryCmdbArgs":
        selected = sum(
            value is not None
            for value in (self.asset_ids, self.ip, self.business_system)
        )
        if selected > 1:
            raise ValueError("asset_ids, ip, business_system 最多提供一个")
        return self


class QueryCmdbDependenciesArgs(_Args):
    asset_id: int = Field(ge=1)
    direction: Literal["up", "down"] = "down"
    max_depth: int = Field(default=3, ge=1, le=5)


class QueryMonitorStatusArgs(_Args):
    target_ids: list[int] | None = Field(default=None, max_length=100)
    ip_prefix: str | None = Field(default=None, min_length=1, max_length=45)
    since_limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def at_most_one_filter(self) -> "QueryMonitorStatusArgs":
        if self.target_ids is not None and self.ip_prefix is not None:
            raise ValueError("target_ids 与 ip_prefix 最多提供一个")
        return self


_ARGUMENT_MODELS: dict[ToolName, type[_Args]] = {
    "kb_glob": KbGlobArgs,
    "kb_grep": KbGrepArgs,
    "kb_read": KbReadArgs,
    "kb_semantic_search": KbSemanticSearchArgs,
    "query_cmdb": QueryCmdbArgs,
    "query_cmdb_dependencies": QueryCmdbDependenciesArgs,
    "query_monitor_status": QueryMonitorStatusArgs,
}

_DESCRIPTIONS: dict[ToolName, str] = {
    "kb_glob": "按 glob 列出 knowledge/ 内文档路径。",
    "kb_grep": "在 knowledge/ 授权范围内用 ripgrep 搜索正文并返回行号。",
    "kb_read": "分页读取 knowledge/ 内一个文档的正文。",
    "kb_semantic_search": "关键词不足时对知识分块做向量语义检索。",
    "query_cmdb": "按资产 ID、IP 或业务系统读取 CMDB 资产。",
    "query_cmdb_dependencies": "按方向和有限深度遍历一个资产的依赖图。",
    "query_monitor_status": "读取目标当前派生状态和有限条最近探测历史。",
}


def tool_schemas_for(allowlist: Iterable[str]) -> list[dict[str, Any]]:
    """Build stable OpenAI-compatible schemas for one role's exact allowlist."""
    schemas: list[dict[str, Any]] = []
    for name in allowlist:
        if name not in _ARGUMENT_MODELS:
            raise ValueError(f"unknown child tool definition: {name!r}")
        tool_name = cast(ToolName, name)
        parameters = deepcopy(_ARGUMENT_MODELS[tool_name].model_json_schema())
        parameters.pop("title", None)
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"[{TOOL_SCHEMA_VERSION}] {_DESCRIPTIONS[tool_name]}",
                    "parameters": parameters,
                },
            }
        )
    return schemas


async def _dispatch_validated(
    db: AsyncSession,
    name: ToolName,
    parsed: _Args,
) -> ToolResult:
    if isinstance(parsed, KbGlobArgs):
        return await kb_glob(parsed.pattern, category=parsed.category)
    if isinstance(parsed, KbGrepArgs):
        return await kb_grep(
            parsed.pattern,
            category=parsed.category,
            context_lines=parsed.context_lines,
        )
    if isinstance(parsed, KbReadArgs):
        return await kb_read(parsed.path, offset=parsed.offset, limit=parsed.limit)
    if isinstance(parsed, KbSemanticSearchArgs):
        return await kb_semantic_search(
            db,
            parsed.query,
            category_id=parsed.category_id,
            top_k=parsed.top_k,
        )
    if isinstance(parsed, QueryCmdbArgs):
        return await query_cmdb(
            db,
            asset_ids=parsed.asset_ids,
            ip=parsed.ip,
            business_system=parsed.business_system,
        )
    if isinstance(parsed, QueryCmdbDependenciesArgs):
        return await query_cmdb_dependencies(
            db,
            parsed.asset_id,
            direction=parsed.direction,
            max_depth=parsed.max_depth,
        )
    if isinstance(parsed, QueryMonitorStatusArgs):
        return await query_monitor_status(
            db,
            target_ids=parsed.target_ids,
            ip_prefix=parsed.ip_prefix,
            since_limit=parsed.since_limit,
        )
    return ToolResult(control="failed", content=f"{name} 参数模型未绑定执行器")


def build_tool_dispatcher(
    db: AsyncSession,
    allowlist: Iterable[str],
) -> ToolDispatcher:
    """Bind one DB session and immutable role allowlist into a loop dispatcher."""
    allowed = frozenset(allowlist)

    async def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in allowed:
            return ToolResult(control="rejected", content=f"工具 {name!r} 不在角色白名单")
        if name not in _ARGUMENT_MODELS:
            return ToolResult(control="rejected", content=f"未知工具 {name!r}")
        tool_name = cast(ToolName, name)
        argument_model = _ARGUMENT_MODELS[tool_name]
        try:
            parsed = argument_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(
                control="clarification",
                content=f"工具 {name!r} 参数无效: {exc.error_count()} 处错误",
            )
        try:
            return await _dispatch_validated(db, tool_name, parsed)
        except Exception as exc:
            return ToolResult(
                control="failed",
                content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
            )

    return dispatch
```

- [ ] **Step 4: Run dispatcher and existing tool tests**

Run:

```bash
uv run pytest tests/test_agent_tool_dispatch.py tests/test_knowledge_tools_fs.py tests/test_knowledge_tools_grep.py tests/test_knowledge_tools_semantic_search.py tests/test_ops_tools_cmdb.py tests/test_ops_tools_monitor.py -v
```

Expected: all pass; no embedding or chat endpoint is contacted because semantic dispatch is not invoked in the new unit tests.

- [ ] **Step 5: Run static checks and commit**

Run:

```bash
uv run mypy app/agent/tool_dispatch.py
uv run ruff check app/agent/tool_dispatch.py tests/test_agent_tool_dispatch.py
```

Expected: both clean.

```bash
git add backend/app/agent/tool_dispatch.py backend/tests/test_agent_tool_dispatch.py
git commit -m "新增子 Agent 只读工具调度器

- 为 T07/T08 七个工具生成 t09-v1 JSON Schema，并用 Pydantic 严格拒绝多余或越界参数
- 调度前再次检查角色白名单，未知或越权工具按 rejected 控制信号失败关闭
- 统一把参数错误转成 clarification、执行异常转成不泄露内部详情的 failed 结果"
```

---

### Task 3: Isolate root and child transcripts with `AgentMessage.agent_id`

**Files:**
- Modify: `backend/app/models/agent_message.py`
- Modify: `backend/app/crud/agent_message.py`
- Modify: `backend/app/agent/session.py`
- Modify: `backend/app/agent/loop.py`
- Create: `backend/alembic/versions/2026_08_11_1800-c5f0a3b8e124_agent_message_agent_id.py`
- Modify: `backend/tests/test_agent_crud_message.py`
- Modify: `backend/tests/test_agent_session.py`
- Modify: `backend/tests/test_agent_loop.py`

**Interfaces:**
- Consumes: existing `AgentSession`, append-only `AgentMessage`, `ChatMessage`, `ToolCall`, and `run_loop` contracts from T06.
- Produces:
  - `AgentMessage.agent_id: str | None`
  - `agent_message_crud.append(..., agent_id: str | None = None) -> AgentMessage`
  - `agent_message_crud.list_for_agent(db, session_id, *, agent_id, limit=None) -> list[AgentMessage]`
  - every session helper gains keyword-only `agent_id: str | None = None`
  - `build_model_history(..., system_prompt: str | None = None) -> list[ChatMessage]`
  - `run_loop(..., agent_id: str | None = None, system_prompt: str | None = None) -> LoopOutcome`

- [ ] **Step 1: Add failing CRUD and history-isolation tests**

Append to `backend/tests/test_agent_crud_message.py`:

```python
async def test_list_for_agent_isolates_root_and_two_children(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await agent_message_crud.append(
        db_session, session_id=session_id, agent_id=None, role="user", content="root"
    )
    await agent_message_crud.append(
        db_session, session_id=session_id, agent_id="child-a", role="user", content="a"
    )
    await agent_message_crud.append(
        db_session, session_id=session_id, agent_id="child-b", role="user", content="b"
    )
    await db_session.commit()

    root = await agent_message_crud.list_for_agent(db_session, session_id, agent_id=None)
    child_a = await agent_message_crud.list_for_agent(
        db_session, session_id, agent_id="child-a"
    )
    all_rows = await agent_message_crud.list_for_session(db_session, session_id)

    assert [row.content for row in root] == ["root"]
    assert [row.content for row in child_a] == ["a"]
    assert [row.content for row in all_rows] == ["root", "a", "b"]
```

Append to `backend/tests/test_agent_session.py`:

```python
async def test_build_model_history_scopes_child_and_prepends_system_prompt(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "root secret")
    await append_user_message(db_session, session_id, "child task", agent_id="child-1")
    await append_assistant_message(
        db_session, session_id, "child answer", agent_id="child-1"
    )
    await db_session.commit()

    history = await build_model_history(
        db_session,
        session_id,
        agent_id="child-1",
        system_prompt="You are the investigator.",
    )

    assert [(message.role, message.content) for message in history] == [
        ("system", "You are the investigator."),
        ("user", "child task"),
        ("assistant", "child answer"),
    ]
```

Append to `backend/tests/test_agent_loop.py`:

```python
async def test_child_loop_never_reads_or_writes_root_transcript(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "root-only-message")
    await append_user_message(
        db_session, session_id, "child-only-task", agent_id="child-1"
    )
    await db_session.commit()

    async def fake_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        assert model_key == "local-chat"
        assert [message.content for message in messages] == [
            "child system",
            "child-only-task",
        ]
        return ChatResult(
            content="child-only-answer",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=2,
            completion_tokens=1,
        )

    async def no_tools(name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError(f"unexpected tool {name!r} with {args!r}")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        agent_id="child-1",
        system_prompt="child system",
        model_key="local-chat",
        dispatch_tool=no_tools,
        chat_fn=fake_chat,
    )
    await db_session.commit()

    root_history = await build_model_history(db_session, session_id)
    child_history = await build_model_history(
        db_session, session_id, agent_id="child-1"
    )
    assert outcome.final_answer == "child-only-answer"
    assert [message.content for message in root_history] == ["root-only-message"]
    assert [message.content for message in child_history] == [
        "child-only-task",
        "child-only-answer",
    ]


async def test_child_loop_reinjects_one_system_prompt_on_every_model_iteration(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "inspect", agent_id="child-1")
    seen: list[list[tuple[str, str]]] = []

    async def fake_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        seen.append([(item.role, item.content) for item in messages])
        if len(seen) == 1:
            return ChatResult(
                content=None,
                tool_calls=[ToolCall(id="call-1", name="kb_read", arguments="{}")],
                finish_reason="tool_calls",
                prompt_tokens=1,
                completion_tokens=1,
            )
        return ChatResult(
            content="done",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(control="ok", content="evidence")

    await run_loop(
        db_session,
        session_id=session_id,
        agent_id="child-1",
        system_prompt="owned instructions",
        model_key="local-chat",
        dispatch_tool=dispatch,
        chat_fn=fake_chat,
    )

    assert len(seen) == 2
    assert all(history[0] == ("system", "owned instructions") for history in seen)
    assert all(sum(role == "system" for role, _ in history) == 1 for history in seen)
```

- [ ] **Step 2: Run the three new tests and verify the contract is absent**

Run:

```bash
uv run pytest tests/test_agent_crud_message.py::test_list_for_agent_isolates_root_and_two_children tests/test_agent_session.py::test_build_model_history_scopes_child_and_prepends_system_prompt tests/test_agent_loop.py::test_child_loop_never_reads_or_writes_root_transcript -v
```

Expected: collection/call failures because `agent_id`, `list_for_agent`, and `system_prompt` do not exist yet.

- [ ] **Step 3: Add the nullable model field and composite lookup index**

Modify the SQLAlchemy imports in `backend/app/models/agent_message.py` to include `Index`, then add the table/index declaration and column exactly as follows:

```python
class AgentMessage(Base):
    """One root- or child-Agent message in a shared user session."""

    __tablename__ = "agent_messages"
    __table_args__ = (
        Index("ix_agent_messages_session_id_agent_id", "session_id", "agent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
```

Keep the existing `role`, `content`, tool-call, timestamp, and `__repr__` fields unchanged. Do not add a foreign key: root uses `NULL`, and transcript history must remain auditable even if a future GC policy removes registry rows.

- [ ] **Step 4: Add the Alembic migration**

Create `backend/alembic/versions/2026_08_11_1800-c5f0a3b8e124_agent_message_agent_id.py`:

```python
"""Scope agent_messages to root or one spawned child.

Revision ID: c5f0a3b8e124
Revises: b3d8f1a4c672
Create Date: 2026-08-11 18:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "c5f0a3b8e124"
down_revision: str | None = "b3d8f1a4c672"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_destructive_downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    op.add_column(
        "agent_messages",
        sa.Column("agent_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_agent_messages_session_id_agent_id",
        "agent_messages",
        ["session_id", "agent_id"],
        unique=False,
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_index("ix_agent_messages_session_id_agent_id", table_name="agent_messages")
    op.drop_column("agent_messages", "agent_id")
```

- [ ] **Step 5: Extend the message CRUD without changing `list_for_session` semantics**

In `backend/app/crud/agent_message.py`, add `agent_id` to `append`, pass it into `AgentMessage`, and add this focused query method. `list_for_session` must continue returning all rows because it is an audit/session-level API.

```python
    async def append(
        self,
        db: AsyncSession,
        *,
        session_id: int,
        role: str,
        content: str,
        agent_id: str | None = None,
        tool_call_id: str | None = None,
        tool_calls: list[dict[str, str]] | None = None,
    ) -> AgentMessage:
        """Append one root- or child-scoped message and flush."""
        message = AgentMessage(
            session_id=session_id,
            agent_id=agent_id,
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            tool_calls=tool_calls,
        )
        db.add(message)
        await db.flush()
        return message

    async def list_for_agent(
        self,
        db: AsyncSession,
        session_id: int,
        *,
        agent_id: str | None,
        limit: int | None = None,
    ) -> list[AgentMessage]:
        """Return only root (`None`) or one exact child's messages, oldest-first."""
        agent_filter = (
            AgentMessage.agent_id.is_(None)
            if agent_id is None
            else AgentMessage.agent_id == agent_id
        )
        stmt = (
            select(AgentMessage)
            .where(AgentMessage.session_id == session_id, agent_filter)
        )
        if limit is not None:
            if limit <= 0:
                return []
            stmt = stmt.order_by(AgentMessage.id.desc()).limit(limit)
        else:
            stmt = stmt.order_by(AgentMessage.id.asc())
        result = await db.execute(stmt)
        messages = list(result.scalars().all())
        if limit is not None:
            messages.reverse()
        return messages
```

The limit must be applied in SQL, then the bounded descending result reversed for model chronology. Do not load an unbounded child transcript and slice in Python.

- [ ] **Step 6: Scope every session helper and prepend the code-owned system prompt**

Replace the public functions in `backend/app/agent/session.py` with these signatures/bodies; keep the module imports and tool-call serialization convention:

```python
async def build_model_history(
    db: AsyncSession,
    session_id: int,
    *,
    agent_id: str | None = None,
    system_prompt: str | None = None,
    max_messages: int = 40,
) -> list[ChatMessage]:
    """Return one exact Agent's bounded history with its code-owned instructions."""
    rows = await agent_message_crud.list_for_agent(
        db, session_id, agent_id=agent_id, limit=max_messages
    )
    history: list[ChatMessage] = []
    if system_prompt is not None:
        history.append(ChatMessage(role="system", content=system_prompt))
    for row in rows:
        tool_calls: list[ToolCall] | None = None
        if row.tool_calls:
            tool_calls = [
                ToolCall(id=call["id"], name=call["name"], arguments=call["arguments"])
                for call in row.tool_calls
            ]
        history.append(
            ChatMessage(
                role=row.role,
                content=row.content,
                tool_call_id=row.tool_call_id,
                tool_calls=tool_calls,
            )
        )
    return history


async def append_user_message(
    db: AsyncSession,
    session_id: int,
    content: str,
    *,
    agent_id: str | None = None,
) -> AgentMessage:
    """Append one user/root-or-parent input to one exact Agent transcript."""
    return await agent_message_crud.append(
        db,
        session_id=session_id,
        agent_id=agent_id,
        role="user",
        content=content,
    )


async def append_assistant_message(
    db: AsyncSession,
    session_id: int,
    content: str,
    *,
    agent_id: str | None = None,
    tool_calls: list[ToolCall] | None = None,
) -> AgentMessage:
    """Append one assistant turn to one exact Agent transcript."""
    serialized = None
    if tool_calls:
        serialized = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in tool_calls
        ]
    return await agent_message_crud.append(
        db,
        session_id=session_id,
        agent_id=agent_id,
        role="assistant",
        content=content,
        tool_calls=serialized,
    )


async def append_tool_result(
    db: AsyncSession,
    session_id: int,
    tool_call_id: str,
    content: str,
    *,
    agent_id: str | None = None,
) -> AgentMessage:
    """Append one correlated tool result to one exact Agent transcript."""
    return await agent_message_crud.append(
        db,
        session_id=session_id,
        agent_id=agent_id,
        role="tool",
        content=content,
        tool_call_id=tool_call_id,
    )
```

- [ ] **Step 7: Thread the scope through `run_loop`**

Add these two keyword arguments to `run_loop` in `backend/app/agent/loop.py`:

```python
    agent_id: str | None = None,
    system_prompt: str | None = None,
```

Then replace its history/append calls with the scoped forms:

```python
        history = await build_model_history(
            db,
            session_id,
            agent_id=agent_id,
            system_prompt=system_prompt,
        )
```

```python
            await append_assistant_message(
                db, session_id, result.content or "", agent_id=agent_id
            )
```

```python
        await append_assistant_message(
            db,
            session_id,
            result.content or "",
            agent_id=agent_id,
            tool_calls=result.tool_calls,
        )
```

Every `append_tool_result` call, including skipped calls, must add `agent_id=agent_id`.

- [ ] **Step 8: Run focused and existing runtime tests**

Run:

```bash
uv run pytest tests/test_agent_crud_message.py tests/test_agent_session.py tests/test_agent_loop.py tests/test_agent_models.py -v
```

Expected: all pass. Existing calls without `agent_id` must still exercise only root (`NULL`) history.

- [ ] **Step 9: Verify the migration graph and static checks**

Run:

```bash
uv run alembic heads
uv run mypy app
uv run ruff check app tests/test_agent_crud_message.py tests/test_agent_session.py tests/test_agent_loop.py
```

Expected: Alembic prints only `c5f0a3b8e124 (head)`; mypy and ruff are clean.

- [ ] **Step 10: Commit**

```bash
git add backend/app/models/agent_message.py backend/app/crud/agent_message.py backend/app/agent/session.py backend/app/agent/loop.py backend/alembic/versions/2026_08_11_1800-c5f0a3b8e124_agent_message_agent_id.py backend/tests/test_agent_crud_message.py backend/tests/test_agent_session.py backend/tests/test_agent_loop.py
git commit -m "为子 Agent 增加独立消息上下文

- 在 agent_messages 增加可空 agent_id 和会话内复合索引，根消息继续使用 NULL，保持现有调用兼容
- CRUD、session helper 与 run_loop 全链路传递 Agent 身份，真正落实 fork_mode=none
- 角色 system prompt 每轮从代码注入，不写进可被用户内容覆盖的历史"
```

---

### Task 4: Make child dollar budgets measurable and reject non-chat models

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/.env.example`
- Modify: `backend/app/core/llm.py`
- Modify: `backend/app/agent/budget.py`
- Modify: `backend/app/agent/loop.py`
- Modify: `backend/tests/test_config.py`
- Modify: `backend/tests/test_agent_llm.py`
- Modify: `backend/tests/test_agent_budget.py`
- Modify: `backend/tests/test_agent_loop.py`

**Interfaces:**
- Consumes: T06 `ModelConfig`, `ChatResult`, `Budget`, and `run_loop`.
- Produces:
  - `ModelConfig.capability: Literal["chat", "embedding"]`
  - `ModelConfig.input_cost_per_million_usd` / `output_cost_per_million_usd`
  - `ChatResult.cost_usd: float`
  - `Budget.reserve_step() -> None` and `Budget.record_cost(cost_usd: float) -> None`
  - `run_loop` charges `ChatResult.cost_usd` immediately after each model response.

- [ ] **Step 1: Add failing price/capability and budget tests**

Append to `backend/tests/test_config.py`:

```python
def test_llm_price_settings_reject_negative_values() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            SECRET_KEY="x" * 32,
            LLM_CHAT_INPUT_COST_PER_MILLION_USD=-0.01,
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_llm_price_settings_reject_non_finite_values(bad: float) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            SECRET_KEY="x" * 32,
            LLM_CHAT_INPUT_COST_PER_MILLION_USD=bad,
        )
```

Add `ValidationError` from `pydantic` and `Settings` from `app.core.config` to that test file's existing imports if they are not already present.

Append to `backend/tests/test_agent_budget.py`:

```python
def test_reserve_step_and_record_cost_enforce_limits_separately() -> None:
    budget = Budget(max_steps=2, max_cost_usd=0.50)

    budget.reserve_step()
    budget.record_cost(0.30)

    with pytest.raises(BudgetExceededError) as exc_info:
        budget.record_cost(0.21)
    assert exc_info.value.limit_name == "max_cost_usd"
```

Append to `backend/tests/test_agent_loop.py`:

```python
async def test_loop_keeps_final_answer_that_crosses_cost_budget(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "expensive question")
    await db_session.commit()

    async def expensive_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        return ChatResult(
            content="answer already incurred cost",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.60,
        )

    async def no_tools(name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError(f"unexpected tool {name!r} with {args!r}")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=no_tools,
        budget=Budget(max_steps=2, max_cost_usd=0.50),
        chat_fn=expensive_chat,
    )

    assert outcome == LoopOutcome(
        reason="final_answer", final_answer="answer already incurred cost"
    )


async def test_loop_does_not_execute_tool_calls_after_cost_budget_is_crossed(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "expensive lookup")
    await db_session.commit()
    dispatched = False

    async def expensive_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        return ChatResult(
            content=None,
            tool_calls=[ToolCall(id="call-1", name="kb_read", arguments="{}")],
            finish_reason="tool_calls",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.60,
        )

    async def must_not_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        nonlocal dispatched
        dispatched = True
        return ToolResult(control="ok", content="unexpected")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=must_not_dispatch,
        budget=Budget(max_steps=2, max_cost_usd=0.50),
        chat_fn=expensive_chat,
    )

    assert outcome == LoopOutcome(reason="budget_exceeded", final_answer=None)
    assert dispatched is False
```

Append this HTTP-level calculation test to `backend/tests/test_agent_llm.py`, reusing that file's existing `httpx.MockTransport` convention:

```python
async def test_chat_reports_configured_token_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ModelConfig(
        name="priced-chat",
        capability="chat",
        base_url="http://model.test/v1",
        api_key="",
        request_model="priced-model",
        input_cost_per_million_usd=2.0,
        output_cost_per_million_usd=8.0,
    )
    monkeypatch.setitem(MODELS, "priced-chat", config)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1_000, "completion_tokens": 500},
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await chat(
            "priced-chat",
            [ChatMessage(role="user", content="hello")],
            client=client,
        )
    finally:
        await client.aclose()

    assert result.cost_usd == pytest.approx(0.006)
```

- [ ] **Step 2: Run the four new tests and verify they fail**

Run (adjust the exact node names if the existing test module groups them in a class):

```bash
uv run pytest tests/test_config.py::test_llm_price_settings_reject_negative_values tests/test_agent_budget.py::test_reserve_step_and_record_cost_enforce_limits_separately tests/test_agent_loop.py::test_loop_keeps_final_answer_that_crosses_cost_budget tests/test_agent_loop.py::test_loop_does_not_execute_tool_calls_after_cost_budget_is_crossed tests/test_agent_llm.py::test_chat_reports_configured_token_cost -v
```

Expected: failures for missing settings, missing `Budget` methods, missing `ChatResult.cost_usd`, and missing `ModelConfig.capability`.

- [ ] **Step 3: Add configurable chat prices**

Add under the LLM section of `Settings` in `backend/app/core/config.py`:

```python
    LLM_CHAT_INPUT_COST_PER_MILLION_USD: float = Field(
        default=0.0, ge=0, allow_inf_nan=False
    )
    LLM_CHAT_OUTPUT_COST_PER_MILLION_USD: float = Field(
        default=0.0, ge=0, allow_inf_nan=False
    )
```

Add after `LLM_CHAT_MODEL` in `backend/.env.example`:

```dotenv
# 按模型供应商当前价格填写；本地模型保持 0
LLM_CHAT_INPUT_COST_PER_MILLION_USD=0
LLM_CHAT_OUTPUT_COST_PER_MILLION_USD=0
```

Do not modify the developer's real `backend/.env`.

- [ ] **Step 4: Add model capability and cost calculation**

In `backend/app/core/llm.py`, import `Literal` and replace `ModelConfig` with:

```python
@dataclass(frozen=True, slots=True)
class ModelConfig:
    """One typed model-registry entry."""

    name: str
    capability: Literal["chat", "embedding"]
    base_url: str
    api_key: str
    request_model: str
    timeout_seconds: float = 60.0
    input_cost_per_million_usd: float = 0.0
    output_cost_per_million_usd: float = 0.0
```

Update the two built-in registrations:

```python
    "local-chat": ModelConfig(
        name="local-chat",
        capability="chat",
        base_url=settings.LLM_CHAT_BASE_URL,
        api_key=settings.llm_chat_api_key,
        request_model=settings.LLM_CHAT_MODEL,
        input_cost_per_million_usd=settings.LLM_CHAT_INPUT_COST_PER_MILLION_USD,
        output_cost_per_million_usd=settings.LLM_CHAT_OUTPUT_COST_PER_MILLION_USD,
    ),
    "local-embedding": ModelConfig(
        name="local-embedding",
        capability="embedding",
        base_url=settings.LLM_EMBEDDING_BASE_URL,
        api_key=settings.llm_embedding_api_key,
        request_model=settings.LLM_EMBEDDING_MODEL,
    ),
```

Add `cost_usd` as the final defaulted field of `ChatResult` so existing fake constructors remain valid:

```python
    cost_usd: float = 0.0
```

Immediately after the unknown-model check in `chat`, reject the wrong capability:

```python
    if config.capability != "chat":
        raise LlmRequestError(f"model {model_key!r} is not registered for chat")
```

Inside `chat`'s response parser, compute and return cost using the response usage:

```python
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        cost_usd = (
            prompt_tokens * config.input_cost_per_million_usd
            + completion_tokens * config.output_cost_per_million_usd
        ) / 1_000_000
        return ChatResult(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice["finish_reason"],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
        )
```

Immediately after the unknown-model check in `embed`, add:

```python
    if config.capability != "embedding":
        raise LlmRequestError(f"model {model_key!r} is not registered for embedding")
```

- [ ] **Step 5: Split step reservation from post-response cost charging**

Replace the methods on `Budget` in `backend/app/agent/budget.py` with:

```python
    def reserve_step(self) -> None:
        """Reserve one model iteration before incurring its external cost."""
        attempted = self.steps_used + 1
        if attempted > self.max_steps:
            raise BudgetExceededError("max_steps", self.max_steps, attempted)
        self.steps_used = attempted

    def record_cost(self, cost_usd: float) -> None:
        """Charge one completed model response and stop after crossing the limit."""
        if not math.isfinite(cost_usd) or cost_usd < 0:
            raise ValueError("cost_usd must be a finite non-negative number")
        self.cost_used_usd += cost_usd
        if self.cost_used_usd > self.max_cost_usd:
            raise BudgetExceededError(
                "max_cost_usd", self.max_cost_usd, self.cost_used_usd
            )

    def record_step(self, cost_usd: float = 0.0) -> None:
        """Backward-compatible combined operation used by existing callers/tests."""
        self.reserve_step()
        self.record_cost(cost_usd)
```

Import `math` in `budget.py`. In `run_loop`, replace `active_budget.record_step()` with `active_budget.reserve_step()`. Immediately after `result = await chat_fn(...)`, charge the response but defer the decision until the response shape is known:

```python
        cost_exceeded = False
        try:
            active_budget.record_cost(result.cost_usd)
        except BudgetExceededError:
            cost_exceeded = True

        if not result.tool_calls:
            await append_assistant_message(
                db, session_id, result.content or "", agent_id=agent_id
            )
            return LoopOutcome(reason="final_answer", final_answer=result.content)

        if cost_exceeded:
            return LoopOutcome(reason="budget_exceeded", final_answer=None)
```

The final-answer branch must therefore occur before the over-budget tool-call branch. Add budget unit cases rejecting `-1`, `float("nan")`, and both infinities so fake/injected model adapters cannot lower or bypass accounting. Add capability tests for both wrong directions: `chat("local-embedding", ...)` and `embed("local-chat", ...)` raise `LlmRequestError` before any HTTP request.

- [ ] **Step 6: Run the focused and complete runtime tests**

Run:

```bash
uv run pytest tests/test_config.py tests/test_agent_llm.py tests/test_agent_budget.py tests/test_agent_loop.py -v
```

Expected: all pass, including old fake `ChatResult` constructors that omit `cost_usd`.

- [ ] **Step 7: Run static checks and commit**

Run:

```bash
uv run mypy app
uv run ruff check app tests/test_config.py tests/test_agent_llm.py tests/test_agent_budget.py tests/test_agent_loop.py
```

Expected: both clean.

```bash
git add backend/app/core/config.py backend/.env.example backend/app/core/llm.py backend/app/agent/budget.py backend/app/agent/loop.py backend/tests/test_config.py backend/tests/test_agent_llm.py backend/tests/test_agent_budget.py backend/tests/test_agent_loop.py
git commit -m "让 Agent 预算按模型用量真实计费

- 为模型登记表增加 chat/embedding 能力标记，阻止把 embedding 模型误用作子 Agent 对话模型
- 按可配置的百万 token 单价计算 ChatResult.cost_usd，本地模型默认零成本
- 将步数预留和响应后计费拆开，子 Agent 超出美元预算后立即停止后续循环"
```

---

### Task 5: Extend the durable registry and configure Spawn limits

**Files:**
- Modify: `backend/app/models/agent_registry.py`
- Modify: `backend/app/crud/agent_registry.py`
- Modify: `backend/app/crud/agent_trace_event.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/.env.example`
- Create: `backend/alembic/versions/2026_08_11_1830-d6a1b4c9f235_agent_registry_spawn_fields.py`
- Modify: `backend/tests/test_agent_models.py`
- Modify: `backend/tests/test_agent_crud_registry.py`
- Modify: `backend/tests/test_agent_crud_trace.py`
- Modify: `backend/tests/test_config.py`
- Create: `backend/tests/test_agent_migration_contract.py`

**Interfaces:**
- Consumes: the existing `AgentRegistry` lifecycle CRUD and Task 3 migration head `c5f0a3b8e124`.
- Produces:
  - durable `trace_id`, `role_version`, `status_changed_at`, and `force_closed` columns;
  - fixed budget JSON keys: `max_steps`, `max_cost_usd`, `max_wall_time_seconds`, `steps_used`, `cost_used_usd`;
  - stable session/children/descendant/TTL queries and conservative session-cost aggregation;
  - all ten `AGENT_*` Spawn settings;
  - deterministic trace ordering by `(step, created_at, id)`.

- [ ] **Step 1: Add failing model, configuration, and CRUD contract tests**

Add assertions to `backend/tests/test_agent_models.py` proving a new `AgentRegistry` has a UUID `trace_id`, `role_version`, timezone-aware `status_changed_at`, and `force_closed is False`.

Append to `backend/tests/test_config.py`:

```python
def test_spawn_limit_defaults_are_bounded() -> None:
    value = Settings(_env_file=None, SECRET_KEY="x" * 32)

    assert value.AGENT_MAX_CONCURRENT_CHILDREN == 5
    assert value.AGENT_MAX_SPAWN_DEPTH == 2
    assert value.AGENT_MAX_CHILDREN_PER_SESSION == 50
    assert value.AGENT_MAX_TOTAL_CHILD_COST_USD == 5.0
    assert value.AGENT_CHILD_MAX_STEPS == 20
    assert value.AGENT_CHILD_MAX_COST_USD == 1.0
    assert value.AGENT_CHILD_MAX_WALL_TIME_SECONDS == 120.0
    assert value.AGENT_CLOSE_TIMEOUT_SECONDS == 5.0
    assert value.AGENT_TERMINAL_RECEIPT_TTL_SECONDS == 300.0
    assert value.AGENT_RECEIPT_GC_INTERVAL_SECONDS == 60.0


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("AGENT_MAX_CONCURRENT_CHILDREN", 0),
        ("AGENT_MAX_SPAWN_DEPTH", 0),
        ("AGENT_MAX_TOTAL_CHILD_COST_USD", -0.01),
        ("AGENT_MAX_TOTAL_CHILD_COST_USD", float("inf")),
        ("AGENT_CHILD_MAX_COST_USD", float("nan")),
        ("AGENT_CHILD_MAX_WALL_TIME_SECONDS", 0),
        ("AGENT_CLOSE_TIMEOUT_SECONDS", 0),
    ],
)
def test_spawn_limits_reject_invalid_values(name: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SECRET_KEY="x" * 32, **{name: bad})
```

Extend `backend/tests/test_agent_crud_registry.py` with focused tests for:

```python
async def test_status_transition_updates_status_changed_at(...): ...
async def test_force_close_is_idempotent_and_structured(...): ...
async def test_list_for_session_is_stable_and_includes_closed(...): ...
async def test_list_descendants_returns_deepest_first(...): ...
async def test_count_for_session_is_cumulative(...): ...
async def test_list_terminal_before_uses_status_changed_at(...): ...
async def test_reserved_cost_uses_active_max_and_terminal_actual(...): ...
```

The aggregation fixture must contain one RUNNING child with `max_cost_usd=1.0` and `cost_used_usd=0.2`, one COMPLETED child with actual `0.3`, and one CLOSED child with actual `0.4`; expect `1.7`. Add a trace test creating two events at the same step and asserting stable `(step, created_at, id)` order.

Create `backend/tests/test_agent_migration_contract.py` as a behavior test, not a source-text grep. Load the revision with `importlib.util.spec_from_file_location`, replace its Alembic `op`/bind boundary with a recording fake that returns a legacy registry row, execute `upgrade()`, and assert:

- `down_revision == "c5f0a3b8e124"`;
- the row update uses `trace_id=child_id`, `status_changed_at=closed_at or created_at`, `role_version="legacy"`, and `force_closed=False`;
- its partial budget is normalized to exactly the five keys while preserving existing maxima;
- all four new columns end with `nullable=False`;
- temporary defaults are cleared from trace/time/version, false remains the force-close server default, and `ix_agent_registry_trace_id` is created after backfill.

The fake records SQLAlchemy/Alembic calls and update parameters; it does not merely inspect the migration file's text and does not connect to SQLite or PostgreSQL.

- [ ] **Step 2: Run the new contracts and observe RED**

Run:

```bash
uv run pytest tests/test_agent_models.py tests/test_agent_crud_registry.py tests/test_agent_crud_trace.py tests/test_agent_migration_contract.py tests/test_config.py -v
```

Expected: failures for missing columns, settings, CRUD queries, structured force-close state, and the trace tiebreakers.

- [ ] **Step 3: Add bounded Spawn settings without touching the real `.env`**

Add to `Settings` in `backend/app/core/config.py`:

```python
    AGENT_MAX_CONCURRENT_CHILDREN: int = Field(default=5, ge=1)
    AGENT_MAX_SPAWN_DEPTH: int = Field(default=2, ge=1)
    AGENT_MAX_CHILDREN_PER_SESSION: int = Field(default=50, ge=1)
    AGENT_MAX_TOTAL_CHILD_COST_USD: float = Field(
        default=5.0, ge=0, allow_inf_nan=False
    )
    AGENT_CHILD_MAX_STEPS: int = Field(default=20, ge=1)
    AGENT_CHILD_MAX_COST_USD: float = Field(
        default=1.0, ge=0, allow_inf_nan=False
    )
    AGENT_CHILD_MAX_WALL_TIME_SECONDS: float = Field(
        default=120.0, gt=0, allow_inf_nan=False
    )
    AGENT_CLOSE_TIMEOUT_SECONDS: float = Field(
        default=5.0, gt=0, allow_inf_nan=False
    )
    AGENT_TERMINAL_RECEIPT_TTL_SECONDS: float = Field(
        default=300.0, ge=0, allow_inf_nan=False
    )
    AGENT_RECEIPT_GC_INTERVAL_SECONDS: float = Field(
        default=60.0, gt=0, allow_inf_nan=False
    )
```

Mirror the ten names and defaults in `backend/.env.example`; do not read or modify `backend/.env`.

- [ ] **Step 4: Add durable registry metadata and a linear migration**

Add these ORM columns in `backend/app/models/agent_registry.py`:

```python
    trace_id: Mapped[str] = mapped_column(
        String(36), nullable=False, default=lambda: str(uuid4()), index=True
    )
    role_version: Mapped[str] = mapped_column(String(30), nullable=False)
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    force_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
```

Import `Boolean`. The new Alembic revision must declare `down_revision = "c5f0a3b8e124"`. For legacy rows, add nullable columns first and backfill exactly:

```text
trace_id         = child_id
role_version     = 'legacy'
status_changed_at = COALESCE(closed_at, created_at)
force_closed     = false
```

In the same per-row backfill, normalize legacy budget JSON without overwriting existing values: default missing `max_steps=20`, `max_cost_usd=1.0`, `max_wall_time_seconds=120.0`, `steps_used=0`, and `cost_used_usd=0.0`. Then alter all four columns to non-null, remove temporary server defaults from trace/time/version, keep a false server default only for `force_closed`, and create a named index for `trace_id`. Downgrade drops the index/columns and must use the repository's existing destructive-downgrade guard.

- [ ] **Step 5: Extend CRUD around one lifecycle clock**

Change `create()` to require `trace_id` and `role_version`, accept an optional caller-generated `child_id` (the manager needs it to build the final path before flush), and initialize the five budget keys. Existing callers that omit `child_id` retain the ORM UUID default. Extend `transition_status()` with an optional complete budget snapshot so terminal status, actual usage, summary, artifacts, and `status_changed_at` flush atomically. Every actual status change must set `status_changed_at = datetime.now(UTC)`. `close(..., force_closed: bool = False)` is idempotent: an already CLOSED row is returned unchanged, while a force-detached row records `force_closed=True` without placing diagnostic text in `result_summary`.

Add these methods, all flush/query only and all scoped by `session_id` where applicable:

```python
async def list_for_session(db, session_id) -> list[AgentRegistry]
async def list_children(db, session_id, parent_agent_id) -> list[AgentRegistry]
async def list_descendants(db, session_id, child_id, *, deepest_first=False) -> list[AgentRegistry]
async def count_for_session(db, session_id) -> int
async def list_terminal_before(db, cutoff) -> list[AgentRegistry]
async def reserved_cost_for_session(db, session_id) -> float
```

Use `(created_at.asc(), child_id.asc())` for stable receipt lists. The descendant walk may be Python-side because the session cap is 50, but it must detect cycles defensively and return descendants before parents when requested. Cost aggregation treats non-CLOSED active states (`REQUESTED`, `SPAWNING`, `RUNNING`) as their full `max_cost_usd`; terminal/CLOSED states use actual `cost_used_usd`.

Update `agent_trace_event_crud.list_for_trace()` ordering to:

```python
.order_by(
    AgentTraceEvent.step.asc(),
    AgentTraceEvent.created_at.asc(),
    AgentTraceEvent.id.asc(),
)
```

- [ ] **Step 6: Run registry tests, migration-head check, and static checks**

Run:

```bash
uv run pytest tests/test_agent_models.py tests/test_agent_crud_registry.py tests/test_agent_crud_trace.py tests/test_agent_migration_contract.py tests/test_config.py -v
uv run alembic heads
uv run mypy app
uv run ruff check app tests/test_agent_models.py tests/test_agent_crud_registry.py tests/test_agent_crud_trace.py tests/test_agent_migration_contract.py tests/test_config.py
```

Expected: focused tests pass, Alembic prints only `d6a1b4c9f235 (head)`, and static checks are clean.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/agent_registry.py backend/app/crud/agent_registry.py backend/app/crud/agent_trace_event.py backend/app/core/config.py backend/.env.example backend/alembic/versions/2026_08_11_1830-d6a1b4c9f235_agent_registry_spawn_fields.py backend/tests/test_agent_models.py backend/tests/test_agent_crud_registry.py backend/tests/test_agent_crud_trace.py backend/tests/test_agent_migration_contract.py backend/tests/test_config.py
git commit -m "扩展可持久化的子 Agent 回执

- 为 registry 增加 trace、角色版本、状态时钟和结构化强制关闭标记，并为旧行定义确定回填
- 增加稳定列举、后代遍历、TTL 查询和保守费用聚合，为 Spawn 配额提供持久化事实源
- 将并发、深度、数量、费用、超时和回收参数纳入强类型配置"
```

---

### Task 6: Implement `SpawnManager`, `ChildReceipt`, and the five primitives

**Files:**
- Create: `backend/app/agent/spawn.py`
- Create: `backend/tests/test_agent_spawn.py`
- Modify: `backend/tests/conftest.py` only if a reusable async session-factory fixture is needed

**Interfaces:**
- Consumes: Tasks 1–5 roles, dispatcher, scoped loop, measurable `Budget`, registry CRUD, trace CRUD, `AsyncSessionLocal`, and configured limits.
- Produces:
  - immutable `ChildBudgetSnapshot`, `ChildReceipt`, and `ChildRunResult`;
  - typed `SpawnRejectedError`, `ChildNotFoundError`, `ChildWaitTimeoutError`, and `ChildRuntimeUnavailableError`;
  - constructible `SpawnManager` for tests plus one production singleton;
  - `spawn_agent`, `wait_agent`, `send_input`, `close_agent`, and `list_agents` methods;
  - isolated default child execution with wall-time enforcement and terminal trace persistence.

- [ ] **Step 1: Write public-contract and validation tests before `spawn.py` exists**

Create `backend/tests/test_agent_spawn.py` and first cover the immutable return contract:

```python
def test_child_receipt_is_immutable() -> None:
    receipt = make_receipt(status="REQUESTED")

    with pytest.raises(FrozenInstanceError):
        receipt.status = "RUNNING"  # type: ignore[misc]
```

Use an isolated SQLite `async_sessionmaker`, the real registry CRUD, and injected fake runners for the async tests. Add separate tests proving all invalid requests fail before a registry row is created:

```python
async def test_spawn_rejects_blank_brief_before_persisting(...): ...
async def test_spawn_rejects_unknown_role_and_non_chat_model(...): ...
async def test_spawn_rejects_tool_allowlist_expansion(...): ...
async def test_spawn_rejects_fork_mode_other_than_none(...): ...
async def test_spawn_rejects_parent_from_another_session(...): ...
async def test_only_reviewer_can_be_nested_and_depth_three_is_rejected(...): ...
async def test_session_child_count_is_cumulative_even_after_close(...): ...
async def test_session_child_budget_is_reserved_conservatively(...): ...
async def test_budget_override_must_only_tighten_configured_child_limits(...): ...
async def test_budget_override_rejects_negative_nan_infinite_and_nonzero_usage(...): ...
```

Each rejection test must assert `await manager.list_agents(session_id) == ()` or that the pre-existing row count is unchanged.

- [ ] **Step 2: Add concurrency, wait, input, terminal-slot, and close tests**

Use `asyncio.Event` gates rather than sleeps:

```python
async def test_two_children_can_be_running_at_the_same_time(...): ...
async def test_sixth_active_child_is_rejected_immediately(...): ...
async def test_completed_child_holds_slot_until_close(...): ...
async def test_close_releases_its_owned_slot_exactly_once(...): ...
async def test_wait_timeout_does_not_cancel_child(...): ...
async def test_wait_returns_persisted_terminal_receipt_without_local_task(...): ...
async def test_wait_reports_runtime_unavailable_for_orphan_active_row(...): ...
async def test_send_input_only_appends_to_a_running_child(...): ...
async def test_close_cancels_running_child_and_is_idempotent(...): ...
async def test_close_force_detaches_child_that_swallows_cancellation(...): ...
async def test_close_parent_closes_descendants_deepest_first(...): ...
```

For the sixth-child test, configure `max_concurrent_children=5`, hold five runner events open, and wrap the sixth call in `asyncio.timeout(0.2)` to prove it rejects instead of queueing. For the wait-timeout test, release the runner after the timeout and verify it can still reach COMPLETED. For `send_input`, query both root (`agent_id=None`) and child transcript scopes.

- [ ] **Step 3: Add child-execution and failure-mapping tests**

Add injected runner cases for COMPLETED, model, tool, policy, infra, cancellation, and wall-time outcomes. Verify:

- each child opens a different `AsyncSession` from the caller and its siblings;
- registry progresses through SPAWNING/RUNNING to the expected terminal state;
- terminal budget contains actual `steps_used` and `cost_used_usd`;
- spawn and terminal trace rows use one `trace_id` and safe `error_class` values;
- a failed transaction is rolled back before a fresh session persists `FAILED/infra`;
- a late runner completion cannot overwrite a receipt already made CLOSED by force detach;
- raw exception messages never enter `result_summary`.

Run RED:

```bash
uv run pytest tests/test_agent_spawn.py -v
```

Expected: collection fails because `app.agent.spawn` does not exist.

- [ ] **Step 4: Define immutable contracts and explicit errors**

Start `backend/app/agent/spawn.py` with these public shapes (use exact project type aliases where mypy requires them):

```python
@dataclass(frozen=True, slots=True)
class ChildBudgetSnapshot:
    max_steps: int
    max_cost_usd: float
    max_wall_time_seconds: float
    steps_used: int = 0
    cost_used_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class ChildReceipt:
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
    status: Literal["COMPLETED", "FAILED"]
    result_summary: str | None
    artifacts: tuple[str, ...] = ()
    error_class: Literal["model", "tool", "policy_reject", "infra"] | None = None


type ChildRunner = Callable[
    [AsyncSession, ChildReceipt, Budget], Awaitable[ChildRunResult]
]
```

Add a single `_to_receipt(row)` conversion that copies JSON lists/dicts into tuples/typed values and never returns an ORM object. Error classes must carry safe machine-readable attributes (`child_id`, limit name, or reason), while their messages must not include secrets.

- [ ] **Step 5: Build session-local runtime ownership and preflight validation**

`SpawnManager.__init__` accepts an `async_sessionmaker[AsyncSession]`, optional `ChildRunner`, injectable `ChatFn` for testing the real default runner (production defaults to `chat`), and explicit limit values defaulting from settings. Expose a read-only `max_concurrent_children` property for workflow wave sizing. Maintain:

```python
@dataclass(slots=True)
class _SessionRuntime:
    lock: asyncio.Lock
    slots: asyncio.BoundedSemaphore
    held_child_ids: set[str]

self._session_runtimes: dict[int, _SessionRuntime]
self._tasks: dict[str, asyncio.Task[None]]
```

Within the per-session lock, `spawn_agent()` validates, in this order:

1. nonblank task brief and `fork_mode == "none"`;
2. known role, registered chat model, and override allowlist is a subset of the role default;
3. same-session, non-CLOSED parent; only reviewer can be nested; calculated depth does not exceed the configured maximum;
4. budget override can only tighten configured child maxima: `1 <= max_steps <= AGENT_CHILD_MAX_STEPS`, finite `0 <= max_cost_usd <= AGENT_CHILD_MAX_COST_USD`, finite `0 < max_wall_time_seconds <= AGENT_CHILD_MAX_WALL_TIME_SECONDS`, and both initial usage fields equal zero;
5. cumulative count, active non-CLOSED count, locally held count, and conservative session cost plus the requested maximum;
6. acquire a slot, create the REQUESTED receipt/task brief/spawn trace, transition SPAWNING, commit, register the named task, and mark the slot as locally held.

The checks and reservation are atomic under the lock, but never await the long-running child task while holding it. Generate `child_id` before CRUD creation; a root child path is `/root/{child_id}` and nested paths append `/{child_id}`. If a pre-commit step after slot acquisition fails, rollback and release that exact slot. If the registry commit succeeds but `asyncio.create_task()` fails, use a fresh transaction to persist `FAILED/infra → CLOSED`, write safe traces, then release the slot; never leave a committed active row without a task.

Register a task done-callback that removes only `_tasks[child_id]` and retrieves any exception to avoid “Task exception was never retrieved”; normal task completion must not release the held slot. A force-detached task gets the same exception-consuming callback and its late writes are blocked by the persisted CLOSED check.

- [ ] **Step 6: Implement the isolated default runner and terminal persistence**

The production runner must:

1. open its own session and re-read the receipt;
2. transition SPAWNING to RUNNING and commit;
3. get the persisted role/version, build the validated dispatcher and exact tool schemas;
4. call `run_loop(..., agent_id=child_id, system_prompt=role.instructions)` inside `asyncio.timeout(max_wall_time_seconds)`;
5. map final answer to COMPLETED; budget/timeout and every `LoopOutcome(reason="early_exit")` control (`rejected`, `clarification`, or `pending_approval`) to FAILED `policy_reject`; uncaught model/tool/infra exceptions to their fixed class;
6. persist actual budget, summary/artifacts, terminal status, and one terminal trace, then commit;
7. leave the semaphore slot held until close.

`ToolResult(control="failed")` remains a model-visible tool message and may be corrected within budget. Only an exception escaping dispatch maps directly to `FAILED/tool`. Add one real-default-runner test with an injected fake `ChatFn` for final completion and one two-iteration test proving `failed` is fed back for correction; separately assert `clarification` becomes terminal `FAILED/policy_reject`. Catch `asyncio.CancelledError` explicitly, persist CANCELLED when the row is not already CLOSED, then re-raise. Any error after a failed DB transaction must rollback and use a fresh session to persist the terminal fallback. Before every late terminal write, re-read the row and skip mutation if it is CLOSED.

- [ ] **Step 7: Implement all five primitives**

Use these public signatures, keeping optional overrides keyword-only:

```python
async def spawn_agent(
    self, *, session_id: int, role: str, task_brief: str,
    trace_id: str | None = None, parent_agent_id: str | None = None,
    model: str | None = None, tools_allowlist: Iterable[str] | None = None,
    budget: ChildBudgetSnapshot | None = None, fork_mode: str = "none",
) -> ChildReceipt: ...

async def wait_agent(self, child_id: str, *, timeout_ms: int | None = None) -> ChildReceipt: ...
async def send_input(self, child_id: str, message: str) -> ChildReceipt: ...
async def close_agent(self, child_id: str) -> ChildReceipt: ...
async def list_agents(self, session_id: int) -> tuple[ChildReceipt, ...]: ...
```

`wait_agent` uses `asyncio.wait_for(asyncio.shield(task), timeout)` so wait timeout never cancels child execution. Terminal receipts return directly; an active receipt with no local task raises `ChildRuntimeUnavailableError`.

`send_input` accepts nonblank text only while the latest persisted state is RUNNING and appends with `agent_id=child_id` in its own committed session.

`close_agent` first obtains the deepest-first descendant list, then calls a non-recursive `_close_one()` for each target. Cancel tasks outside the session lock. After `task.cancel()`, wait with `await asyncio.wait_for(asyncio.shield(task), close_timeout)` (or equivalent `asyncio.wait`); never call bare `wait_for(task)`, because a coroutine that swallows cancellation can make that API wait beyond the deadline. On timeout, persist a CANCELLED terminal trace first, then `CLOSED` with `force_closed=True`; its late writes see CLOSED and do nothing.

Reacquire the session lock for slot accounting and use the exact guarded operation:

```python
if child_id in runtime.held_child_ids:
    runtime.held_child_ids.remove(child_id)
    runtime.slots.release()
```

Never use unconditional `remove`, and never pair `discard` with an unconditional release. Write a close trace only for an actual state change, not repeated no-op calls. Tests must prove the force-detach deadline returns, CANCELLED precedes close, the late task cannot overwrite CLOSED, and a second close neither adds trace rows nor releases again.

- [ ] **Step 8: Run Spawn tests and adjacent regression tests**

Run:

```bash
uv run pytest tests/test_agent_spawn.py tests/test_agent_crud_registry.py tests/test_agent_crud_message.py tests/test_agent_session.py tests/test_agent_loop.py -v
```

Expected: all pass without a real model, embedding service, or PostgreSQL container.

- [ ] **Step 9: Run static checks and commit**

Run:

```bash
uv run mypy app
uv run ruff check app tests/test_agent_spawn.py
```

Expected: clean.

```bash
git add backend/app/agent/spawn.py backend/tests/test_agent_spawn.py backend/tests/conftest.py
git commit -m "实现进程内子 Agent Spawn 运行时

- 用不可变 ChildReceipt 和持久化 registry 暴露 spawn、wait、send、close、list 五个原语
- 以 session 锁、五槽信号量和本地持槽集合保证并发上限及只释放一次
- 为独立 child session、墙钟预算、失败映射、级联关闭和生命周期 trace 增加确定性测试"
```

Only include `backend/tests/conftest.py` in `git add` if this task actually changed it.


---

### Task 7: Reconcile orphaned receipts, run terminal GC, and wire application lifespan

**Files:**
- Modify: `backend/app/agent/spawn.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_agent_recovery.py`
- Create: `backend/tests/test_main.py`

**Interfaces:**
- Consumes: Task 6 manager ownership, Task 5 TTL query, existing monitor/CMDB background loops, and FastAPI lifespan.
- Produces:
  - `SpawnManager.reconcile_startup() -> tuple[ChildReceipt, ...]`;
  - `SpawnManager.collect_expired_receipts() -> tuple[ChildReceipt, ...]`;
  - `SpawnManager.shutdown() -> None`;
  - `run_receipt_gc_loop(manager) -> None`;
  - one configured process-wide `spawn_manager` used by the application lifespan.

- [ ] **Step 1: Write deterministic recovery and GC tests**

Create `backend/tests/test_agent_recovery.py` with direct DB fixtures for every state:

```python
async def test_startup_reconciliation_closes_orphan_active_rows(...): ...
async def test_startup_reconciliation_closes_terminal_rows(...): ...
async def test_reconciliation_does_not_release_unowned_semaphore_slots(...): ...
async def test_gc_closes_only_terminal_receipts_older_than_ttl(...): ...
async def test_gc_releases_a_locally_owned_terminal_slot(...): ...
async def test_shutdown_cancels_and_closes_all_local_children(...): ...
async def test_recovery_is_idempotent(...): ...
```

Freeze/pass `now` into the collection helper rather than sleeping. Active orphan rows must pass `RUNNING → CANCELLED → CLOSED`; REQUESTED/SPAWNING use their legal cancellation path. Terminal rows go directly CLOSED. All recovery close traces use `error_class="infra"` only where the process lost runtime ownership; no exception details are appended to result summaries.

- [ ] **Step 2: Write a lifespan-order test**

Create `backend/tests/test_main.py` by monkeypatching the monitor loop, CMDB diff loop, GC loop, manager reconciliation/shutdown, and engine dispose with event-recording fakes. Enter and exit `lifespan(app)` and assert:

```python
assert events.index("reconcile") < events.index("yielded")
assert events.index("gc-cancelled") < events.index("spawn-shutdown")
assert events.index("spawn-shutdown") < events.index("engine-dispose")
```

The test must also prove background-task cancellation is awaited and does not leak an `asyncio.Task`.

- [ ] **Step 3: Run the tests and observe RED**

Run:

```bash
uv run pytest tests/test_agent_recovery.py tests/test_main.py -v
```

Expected: failures for missing recovery methods/loop and old lifespan ordering.

- [ ] **Step 4: Implement startup reconciliation and terminal GC**

In `SpawnManager.reconcile_startup()`, query all non-CLOSED rows at startup because this fresh manager owns no tasks. Close descendants before parents. Use legal transitions where possible, then structured close; do not call `slots.release()` unless `child_id` is in this manager's `held_child_ids`.

In `collect_expired_receipts(now: datetime | None = None)`, calculate the UTC cutoff from `terminal_receipt_ttl_seconds`, query only COMPLETED/FAILED/CANCELLED rows with old `status_changed_at`, and call the same idempotent close path. Registry rows and transcripts remain stored; T09 GC is lifecycle cleanup, not physical deletion.

Add:

```python
async def run_receipt_gc_loop(manager: SpawnManager) -> None:
    while True:
        try:
            await manager.collect_expired_receipts()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("子 Agent 回执 GC 失败")
        await asyncio.sleep(manager.receipt_gc_interval_seconds)
```

`shutdown()` snapshots local child IDs, closes roots (which cascade), then any remaining IDs, and finally asserts/clears only empty task/held maps. It is safe to call twice.

- [ ] **Step 5: Wire the production singleton into lifespan**

Construct `spawn_manager` once in `spawn.py` from `AsyncSessionLocal` and the ten settings; tests continue constructing independent managers. In `backend/app/main.py`:

1. `await spawn_manager.reconcile_startup()` before starting background jobs and before `yield`;
2. start monitor, CMDB diff, and receipt GC tasks;
3. on shutdown, cancel and await all three background loops;
4. `await spawn_manager.shutdown()`;
5. `await engine.dispose()` last.

Keep the existing monitor/CMDB behavior unchanged apart from the shared cleanup list.

- [ ] **Step 6: Verify recovery plus existing service lifecycles**

Run:

```bash
uv run pytest tests/test_agent_recovery.py tests/test_main.py tests/test_monitor_sweep.py tests/test_cmdb_diff.py -v
uv run mypy app
uv run ruff check app/main.py app/agent/spawn.py tests/test_agent_recovery.py tests/test_main.py
```

Expected: all pass and static checks are clean.

- [ ] **Step 7: Commit**

```bash
git add backend/app/agent/spawn.py backend/app/main.py backend/tests/test_agent_recovery.py backend/tests/test_main.py
git commit -m "接入子 Agent 启动对账与回执回收

- 启动时关闭失去运行时所有权的孤儿和遗留终态回执，不虚假释放本进程槽位
- 用可配置 TTL 回收终态占槽并保留 registry 与 transcript 审计数据
- 将 GC、子任务级联关闭和数据库释放按确定顺序接入 FastAPI lifespan"
```

---

### Task 8: Implement bounded batch-classification and root-cause workflows

**Files:**
- Create: `backend/app/agent/orchestration.py`
- Create: `backend/tests/test_agent_orchestration.py`

**Interfaces:**
- Consumes: only the five typed Spawn primitives, not manager internals or ORM rows.
- Produces:
  - strict Pydantic input/output models for classification, investigation, and review;
  - a minimal `SpawnController` protocol for fake-controller unit tests;
  - `classify_documents(...) -> BatchClassificationOutcome`;
  - `investigate_root_cause(...) -> RootCauseOutcome`;
  - bounded waves sized by `controller.max_concurrent_children` and leak-free `finally` close.

- [ ] **Step 1: Define failing strict-schema tests**

Create `backend/tests/test_agent_orchestration.py`. Test the following Pydantic models with `ConfigDict(extra="forbid", strict=True)`:

```python
class _StrictWorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ClassificationDocument(_StrictWorkflowModel):
    document_id: int
    title: str
    file_path: str
    current_category: str | None = None


class ClassificationResult(_StrictWorkflowModel):
    document_id: int
    recommended_category: str
    confidence: float = Field(ge=0, le=1)
    needs_review: bool
    reason: str


class ClassificationReview(_StrictWorkflowModel):
    summary: str
    accepted_document_ids: list[int]
    disputed_document_ids: list[int]
    recommended_actions: list[str]


class RootCauseBranch(_StrictWorkflowModel):
    name: str
    objective: str


class InvestigationFinding(_StrictWorkflowModel):
    branch: str
    hypothesis: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str]
    gaps: list[str]
    next_checks: list[str]


class ReviewSynthesis(_StrictWorkflowModel):
    summary: str
    likely_causes: list[str]
    evidence_gaps: list[str]
    recommended_next_steps: list[str]
```

Add parse tests rejecting extra keys, numeric strings such as `"0.9"`, missing keys, invalid confidence, non-object JSON, and reviewer JSON with the wrong shape.

- [ ] **Step 2: Add fake-controller tests for batch classification**

The fake controller records spawn/wait/close calls and exposes a configurable `max_concurrent_children`. Add:

```python
async def test_single_document_is_rejected_without_spawn(...): ...
async def test_two_documents_spawn_in_parallel_without_unneeded_reviewer(...): ...
async def test_more_than_five_documents_run_in_bounded_waves(...): ...
async def test_confidence_below_point_eight_triggers_reviewer(...): ...
async def test_confidence_equal_to_point_eight_does_not_trigger_reviewer(...): ...
async def test_needs_review_new_category_parse_failure_or_child_failure_triggers_reviewer(...): ...
async def test_all_classifiers_failed_returns_workflow_failure_without_reviewer(...): ...
async def test_classification_closes_every_spawned_child_when_wait_or_parse_fails(...): ...
async def test_close_failure_still_closes_siblings_and_prevents_success(...): ...
```

For wave tests, assert the sixth classifier is not spawned until the first five have all been waited and closed. The threshold is exactly `< 0.80`; `0.80` itself is not low confidence. A category outside a nonempty `allowed_categories` set is a new-category suggestion and requires review. The outcome preserves `failed_child_ids`, parse failures with a safely truncated raw summary, and all successfully parsed suggestions.

- [ ] **Step 3: Add fake-controller tests for root-cause investigation**

Add:

```python
async def test_default_root_cause_branches_are_parallel_and_read_only(...): ...
async def test_custom_workflow_requires_at_least_two_branches(...): ...
async def test_partial_branch_failure_still_spawns_reviewer(...): ...
async def test_all_branches_failed_skips_reviewer_and_reports_failure(...): ...
async def test_malformed_reviewer_result_is_an_explicit_workflow_failure(...): ...
async def test_root_cause_closes_investigators_and_reviewer_on_cancellation(...): ...
```

Assert default branch names are `monitor_history`, `cmdb_topology`, and `peer_scope`; their task briefs mention that unavailable change-log evidence must be reported as a gap rather than queried via raw SQL.

Run RED:

```bash
uv run pytest tests/test_agent_orchestration.py -v
```

Expected: collection fails because `app.agent.orchestration` does not exist.

- [ ] **Step 4: Define the controller boundary and immutable workflow outcomes**

Use this narrow protocol; workflows must not reach `_tasks`, semaphores, CRUD, or SQLAlchemy:

```python
class SpawnController(Protocol):
    @property
    def max_concurrent_children(self) -> int: ...

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
    ) -> ChildReceipt: ...
    async def wait_agent(self, child_id: str, *, timeout_ms: int | None = None) -> ChildReceipt: ...
    async def close_agent(self, child_id: str) -> ChildReceipt: ...
```

Import `Iterable` from `collections.abc` and `ChildBudgetSnapshot` from `spawn.py`. Use frozen dataclasses for `SpawnRequest`, `ParseFailure`, `WaveResult`, `BatchClassificationOutcome`, and `RootCauseOutcome`, plus a typed `WorkflowCleanupError` carrying only child IDs whose close calls failed. Define the request exactly:

```python
@dataclass(frozen=True, slots=True)
class SpawnRequest:
    session_id: int
    role: str
    task_brief: str
    trace_id: str
    parent_agent_id: str | None = None
    model: str | None = None
    tools_allowlist: tuple[str, ...] | None = None
    budget: ChildBudgetSnapshot | None = None
```

It does not use an untyped `**kwargs` bag. Outcomes include `trace_id`, successful structured results, every child ID, failed child IDs, parse failures, optional reviewer result, and optional `workflow_failure`. Do not return ORM rows.

- [ ] **Step 5: Implement one bounded wave helper**

Implement one private helper used by both workflows:

```python
async def _run_wave(
    controller: SpawnController,
    spawn_requests: Sequence[SpawnRequest],
) -> WaveResult:
    spawned: list[ChildReceipt] = []
    # Spawn each request, then wait with return_exceptions=True so one runtime
    # wait failure never abandons siblings. Convert exceptions to safe child-ID
    # failure records; never put raw exception text in a workflow outcome.
    ...
```

Implement cleanup as a separate `_close_all()` that always attempts every spawned child with `gather(..., return_exceptions=True)`, inspects every result, and raises `WorkflowCleanupError(failed_child_ids)` if any close failed. `_run_wave` must never return `WaveResult` after such an error. If both work and cleanup fail, preserve both with exception chaining or an `ExceptionGroup`; do not silently replace one. If the parent workflow is cancelled, run `_close_all()` in a shielded cleanup task and then re-raise `CancelledError`; log any cleanup failure by child ID without leaking raw details. The close-failure test must prove later siblings were still closed and no success outcome was returned.

`WaveResult` contains successfully retrieved receipts plus safe `(child_id, failure_kind)` wait failures. Chunk requests by `controller.max_concurrent_children`; never optimistically issue a sixth Spawn and wait for capacity. If Spawn itself fails after earlier receipts were created, close all already-created receipts before propagating a typed workflow failure.

- [ ] **Step 6: Implement batch classification**

Use this public signature:

```python
async def classify_documents(
    controller: SpawnController,
    *,
    session_id: int,
    documents: Sequence[ClassificationDocument],
    allowed_categories: Sequence[str] = (),
) -> BatchClassificationOutcome: ...
```

Define `CLASSIFICATION_LOW_CONFIDENCE_THRESHOLD = 0.80`; it is a versioned workflow contract, not a caller override. Reject fewer than two documents and duplicate `document_id` values before generating a trace ID or spawning. One classifier receives only its ID/title/path/current category and allowed category codes; it reads content through its role tools. Parse `result_summary` with `ClassificationResult.model_validate_json()` and verify the returned document ID matches the assigned input.

Spawn a root-level reviewer only when at least one valid classification exists and any result is low-confidence, flagged, outside allowed categories, disputed, or accompanied by a failure/parse failure. Reviewer input contains bounded classification summaries, failure markers, and evidence paths—not child transcripts. Strictly parse its `ClassificationReview`. This workflow is advisory and must contain no knowledge-document update or filesystem move.

- [ ] **Step 7: Implement multi-branch root-cause investigation**

Use:

```python
DEFAULT_ROOT_CAUSE_BRANCHES: tuple[RootCauseBranch, ...] = (...)

async def investigate_root_cause(
    controller: SpawnController,
    *,
    session_id: int,
    incident_context: str,
    branches: Sequence[RootCauseBranch] = DEFAULT_ROOT_CAUSE_BRANCHES,
) -> RootCauseOutcome: ...
```

Reject blank incident context, fewer than two branches, duplicate/blank branch names, and blank objectives before Spawn. Spawn one investigator per branch in bounded waves, strictly parse summaries, and validate that each returned `branch` matches its assignment. If all fail, set `workflow_failure` and skip reviewer. Otherwise spawn one root-level reviewer with only bounded findings/failures, parse `ReviewSynthesis`, and report malformed/missing review as an explicit workflow failure while preserving successful findings.

- [ ] **Step 8: Verify workflows and commit**

Run:

```bash
uv run pytest tests/test_agent_orchestration.py -v
uv run mypy app/agent/orchestration.py
uv run ruff check app/agent/orchestration.py tests/test_agent_orchestration.py
```

Expected: all pass and static checks are clean.

```bash
git add backend/app/agent/orchestration.py backend/tests/test_agent_orchestration.py
git commit -m "新增两个有界并行 Agent 编排范式

- 批量文档按运行时并发上限分波归类，并在低置信、冲突或失败时调用 reviewer
- 根因排查并行运行监控、拓扑和同范围证据分支，再由 reviewer 严格综合
- 所有输出使用严格结构校验且所有异常路径 finally close，避免终态回执长期占槽"
```

---

### Task 9: Prove cross-component invariants and complete T09 verification

**Files:**
- Create: `backend/tests/test_agent_spawn_integration.py`
- Modify: `docs/superpowers/specs/2026-08-11-t09-spawn-orchestration-design.md` only if implementation-driven wording corrections remain
- Modify: `docs/superpowers/plans/2026-08-11-t09-spawn-orchestration.md` to check completed steps during execution

**Interfaces:**
- Consumes: the complete T09 runtime and both workflows.
- Produces: a repeatable SQLite/fake-runner acceptance test and a clean full-suite/static/migration-head verification record, with no real API or Docker database use.

- [ ] **Step 1: Write one real-manager integration harness**

Create `backend/tests/test_agent_spawn_integration.py` using:

- the repository's real async SQLite ORM setup and all real models;
- a real `SpawnManager` with limits matching production defaults;
- the real registry, message, trace, role, and workflow code;
- the production default child runner with an injected deterministic fake `ChatFn`; the fake records the exact model histories it receives and returns strict JSON based on the role/task brief, so the real scoped loop, dispatcher construction, budget accounting, and terminal mapping remain exercised;
- `asyncio.Event` gates so workflow concurrency is observed, not inferred from elapsed time.

Do not patch registry CRUD, message CRUD, the default child runner, workflow parsing, or manager lifecycle methods in this test. The fake chat must increment observable concurrency with event gates, return complete `ChatResult` usage/cost fields, and never perform HTTP.

- [ ] **Step 2: Add the batch-classification invariant test**

Run six classification inputs so two waves are required, with one low-confidence result that triggers reviewer. After the workflow returns, assert:

```python
assert len(outcome.suggestions) == 6
assert outcome.review is not None
assert max_observed_running == 5
assert all(receipt.status == "CLOSED" for receipt in await manager.list_agents(session_id))
assert await agent_registry_crud.list_active_children(db, session_id) == []
```

Also assert root message history contains neither classifier task briefs nor child summaries; every classifier's captured history contains its own task brief and none of its siblings' file paths; every receipt has spawn, terminal, and close traces; and its final budget JSON contains actual usage.

- [ ] **Step 3: Add the root-cause partial-failure invariant test**

Make one investigator fail and two return valid findings. Assert sibling work continues, reviewer receives only bounded structured findings/failure markers, outcome preserves the failed child ID, all children including reviewer become CLOSED, and a second idempotent close does not change trace count or release a slot twice.

- [ ] **Step 4: Run the integration test and repair only T09 defects**

Run:

```bash
uv run pytest tests/test_agent_spawn_integration.py -v
```

Expected: pass. If it exposes a defect, first add the smallest focused regression test in the owning Task 1–8 test module, observe it fail, fix the owning module, then rerun both that module and the integration test. Do not refactor adjacent RBAC/CMDB/knowledge code.

- [ ] **Step 5: Run the complete automated verification**

From `backend/`, run exactly:

```bash
uv run pytest -v
uv run mypy app
uv run ruff check .
uv run alembic heads
```

Expected:

- all tests pass with no live model, embedding, or PostgreSQL connection;
- mypy and ruff exit zero;
- Alembic reports exactly `d6a1b4c9f235 (head)`;
- no pending `asyncio.Task` warnings or unclosed SQLAlchemy/httpx resources appear.

- [ ] **Step 6: Review the final diff against both architecture documents**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Audit every changed line against these acceptance points:

1. no external write tool or hidden audit/raw-SQL path was added;
2. root and sibling transcripts are isolated by DB query scope;
3. receipt state, role version, force-close state, budgets, and traces are durable;
4. terminal receipts hold slots until close, and all workflow paths close them;
5. restart recovery never pretends to resume an in-flight model call;
6. both workflows are bounded, strict, partial-failure aware, and advisory;
7. no real `.env`, secret, generated database, cache, or coverage artifact is staged.

- [ ] **Step 7: Commit integration acceptance**

```bash
git add backend/tests/test_agent_spawn_integration.py docs/superpowers/specs/2026-08-11-t09-spawn-orchestration-design.md docs/superpowers/plans/2026-08-11-t09-spawn-orchestration.md
git commit -m "完成 T09 Spawn 编排跨组件验收

- 用真实 SQLite ORM、SpawnManager 和两个 workflow 验证并发分波、部分失败与零活跃槽泄漏
- 证明 root、child 与 sibling 消息隔离，且每个 ChildReceipt 的预算和生命周期 trace 完整落盘
- 记录全量 pytest、mypy、ruff 与单 Alembic head 验证结果，不依赖真实外部服务"
```

Only stage the two docs if they changed during implementation; never create an empty documentation change merely to match the command.

---

## After All Tasks

- Use `superpowers:verification-before-completion` and report the fresh command outputs, not remembered results.
- Dispatch one final `superpowers:requesting-code-review` reviewer across the complete T09 diff. Fix Critical/Important findings with RED/GREEN regression tests and rerun the full verification.
- Confirm `git status --short` contains no unintended files. Work remains on `master`; do not create a branch, PR, or push.
- Do not claim T09 complete until Tasks 1–9 each have their focused commit and the full acceptance matrix is green.
