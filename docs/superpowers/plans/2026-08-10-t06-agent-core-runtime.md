# T06 · Agent 内核基建 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Agent runtime foundation — data models, CRUD, the model registry (`llm.py`), budget tracking, session/transcript management, and the standard agent loop — that every later subsystem (knowledge base, CMDB/monitoring tools, spawn orchestration, HITL) will be built on top of.

**Architecture:** New top-level `app/agent/` package (peer to `app/api`, `app/crud`, `app/models`, `app/services`) holds the loop, session helpers, and budget tracker. Five new SQLAlchemy models back it (`AgentSession`, `AgentMessage`, `AgentRegistry`, `HitlProposal`, `AgentTraceEvent`). `app/core/llm.py` is the single choke point for model calls. Nothing in this plan adds HTTP routes or a WebSocket endpoint — everything is exercised through direct async function calls and tested without a live LLM (httpx `MockTransport`) or live Postgres (existing aiosqlite test fixtures).

**Tech Stack:** Python 3.14.3, FastAPI, SQLAlchemy 2 async, PostgreSQL + Alembic, httpx, pytest + pytest-asyncio, uv.

**Reference docs:** [docs/AGENT_ARCHITECTURE.md](../../AGENT_ARCHITECTURE.md) §3 (data model), §4 (tool contracts — `loop.py`'s `ToolResult`/`control` shape), §10 (trace fields); [docs/guide.md](../../guide.md) §2 (standard loop), §6.1 (session model), §9.1 (budget).

## Global Constraints

- Python `>=3.14,<3.15` only; every command runs as `uv run <cmd>` from `backend/` — never bare `python`/`pytest`.
- mypy strict mode is on (`[tool.mypy] strict = true`) — every function, including test helpers, needs complete type hints and an explicit return type.
- PEP 695 syntax is expected and used throughout this codebase: `type Alias = ...` for type aliases, `def func[T](...)` for generic functions.
- ruff: line-length 100, rule sets `E, F, W, I, N, B, UP, ASYNC` (`uv run ruff check .` must be clean).
- CRUD methods only `db.flush()` — they never `db.commit()`. Nothing in this plan calls `db.commit()` (no route layer exists yet in T06); tests commit explicitly via fixtures or `await db.commit()` where a durable row is needed across a fixture boundary.
- All timestamp columns: `Mapped[datetime]` with `default=lambda: datetime.now(UTC)` **and** `server_default=func.now()` together (see `TimestampMixin` / `user_roles.created_at`).
- Soft delete is duck-typed via a literal `is_deleted` column name — none of the five new models declare one (none are soft-deletable in this plan), so `CRUDBase.soft_delete()` is simply unused for them.
- User-facing strings (docstring rationale, error messages surfaced to a caller) may be Chinese; code identifiers and "why" comments are English, matching the existing codebase mix.
- Every response envelope / HTTP concern is **out of scope** for this plan — no `ResponseEnvelope`, no routes, no permission codes. Those land when the API layer is built in a later plan (T10/T11 per `docs/AGENT_ARCHITECTURE.md`).
- Test runner: `uv run pytest tests/<file>.py -v` (from `backend/`). Type/lint check: `uv run mypy app` and `uv run ruff check .`.
- Commit messages: Chinese, a concise title line, blank line, then bullet points explaining what changed and why (this project's convention — see `CLAUDE.md`). **Never** add a `Co-Authored-By` line.

---

### Task 1: Data models + Alembic migration

**Files:**
- Create: `backend/app/models/agent_session.py`
- Create: `backend/app/models/agent_message.py`
- Create: `backend/app/models/agent_registry.py`
- Create: `backend/app/models/hitl_proposal.py`
- Create: `backend/app/models/agent_trace_event.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/2026_08_10_1500-e1a4c7d9f215_agent_core_runtime.py`
- Test: `backend/tests/test_agent_models.py`

**Interfaces:**
- Consumes: `app.models.base.Base`, `app.models.base.TimestampMixin`, `app.models.user.User` (FK target only).
- Produces (used by every later task in this plan):
  - `AgentSession(id, user_id, title, status, created_at, updated_at)`
  - `AgentMessage(id, session_id, role, content, tool_call_id, tool_calls, created_at)` — `tool_calls: list[dict[str, str]] | None`, each dict shaped `{"id": ..., "name": ..., "arguments": ...}`
  - `AgentRegistry(child_id, session_id, parent_agent_id, agent_path, role, model, tools_allowlist, sandbox_mode, task_brief, budget, status, result_summary, artifacts, created_at, closed_at)`
  - `HitlProposal(id, session_id, proposed_by_agent_id, action_type, action_payload, status, reviewed_by_user_id, reviewed_at, executed_at, created_at)`
  - `AgentTraceEvent(id, trace_id, session_id, agent_id, parent_agent_id, step, span_type, tool, control, cost_usd, latency_ms, error_class, created_at)`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_models.py`:

```python
"""Structural tests for the new agent-runtime ORM models."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_message import AgentMessage
from app.models.agent_registry import AgentRegistry
from app.models.agent_session import AgentSession
from app.models.agent_trace_event import AgentTraceEvent
from app.models.hitl_proposal import HitlProposal
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_agent_session_round_trip(db_session: AsyncSession, test_user: User) -> None:
    session = AgentSession(user_id=test_user.id, title="网段巡检", status="active")
    db_session.add(session)
    await db_session.commit()

    result = await db_session.execute(select(AgentSession).where(AgentSession.id == session.id))
    stored = result.scalar_one()
    assert stored.status == "active"
    assert stored.user_id == test_user.id


async def test_agent_message_stores_tool_calls_json(
    db_session: AsyncSession, test_user: User
) -> None:
    session = AgentSession(user_id=test_user.id, title="", status="active")
    db_session.add(session)
    await db_session.flush()

    message = AgentMessage(
        session_id=session.id,
        role="assistant",
        content="",
        tool_calls=[{"id": "call_1", "name": "kb_grep", "arguments": "{}"}],
    )
    db_session.add(message)
    await db_session.commit()

    result = await db_session.execute(select(AgentMessage).where(AgentMessage.id == message.id))
    stored = result.scalar_one()
    assert stored.tool_calls == [{"id": "call_1", "name": "kb_grep", "arguments": "{}"}]
    assert stored.tool_call_id is None


async def test_agent_registry_defaults_to_requested_status(
    db_session: AsyncSession, test_user: User
) -> None:
    session = AgentSession(user_id=test_user.id, title="", status="active")
    db_session.add(session)
    await db_session.flush()

    child = AgentRegistry(
        session_id=session.id,
        parent_agent_id=None,
        agent_path="/root/kb_explorer",
        role="kb_explorer",
        model="local-chat",
        tools_allowlist=["kb_grep", "kb_read"],
        sandbox_mode="read-only",
        task_brief="查找 SOP 中关于交换机重启的章节",
        budget={"max_steps": 10, "max_cost_usd": 0.5},
    )
    db_session.add(child)
    await db_session.commit()

    result = await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.child_id == child.child_id)
    )
    stored = result.scalar_one()
    assert stored.status == "REQUESTED"
    assert stored.closed_at is None
    assert stored.tools_allowlist == ["kb_grep", "kb_read"]


async def test_hitl_proposal_defaults_to_pending(db_session: AsyncSession, test_user: User) -> None:
    session = AgentSession(user_id=test_user.id, title="", status="active")
    db_session.add(session)
    await db_session.flush()

    proposal = HitlProposal(
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "交换机 SW-12 离线"},
    )
    db_session.add(proposal)
    await db_session.commit()

    result = await db_session.execute(select(HitlProposal).where(HitlProposal.id == proposal.id))
    stored = result.scalar_one()
    assert stored.status == "PENDING"
    assert stored.reviewed_at is None


async def test_agent_trace_event_records_span(db_session: AsyncSession, test_user: User) -> None:
    session = AgentSession(user_id=test_user.id, title="", status="active")
    db_session.add(session)
    await db_session.flush()

    event = AgentTraceEvent(
        trace_id="trace-1",
        session_id=session.id,
        agent_id="root",
        parent_agent_id=None,
        step=1,
        span_type="tool",
        tool="kb_grep",
        control="ok",
        cost_usd=0.001,
        latency_ms=120,
    )
    db_session.add(event)
    await db_session.commit()

    result = await db_session.execute(
        select(AgentTraceEvent).where(AgentTraceEvent.trace_id == "trace-1")
    )
    stored = result.scalar_one()
    assert stored.span_type == "tool"
    assert stored.error_class is None
    assert isinstance(stored.created_at, datetime)
    assert stored.created_at.tzinfo is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.agent_session'` (or equivalent import error).

- [ ] **Step 3: Write the model files**

Create `backend/app/models/agent_session.py`:

```python
"""Agent chat session — one conversation between a user and the ops agent."""

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AgentSession(Base, TimestampMixin):
    """One chat conversation between a user and the ops agent."""

    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    def __repr__(self) -> str:
        return f"<AgentSession(id={self.id}, user_id={self.user_id}, status={self.status!r})>"
```

Create `backend/app/models/agent_message.py`:

```python
"""Append-only transcript of one agent session.

`tool_calls` carries the raw tool-call requests attached to an *assistant* row
(list of ``{"id", "name", "arguments"}`` dicts) so the exact request can be
replayed into the next model call. `tool_call_id` is used the other direction —
on a *tool* row, it names which call this row is the result of. A row never
uses both.
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentMessage(Base):
    """One message (user/assistant/tool) in an agent session's transcript."""

    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_calls: Mapped[list[dict[str, str]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"<AgentMessage(id={self.id}, session_id={self.session_id}, role={self.role!r})>"
```

Create `backend/app/models/agent_registry.py`:

```python
"""Child-agent registry — the durable ChildReceipt store for dynamic spawn.

`child_id` is a string (UUID4), not an autoincrement int, mirroring the
existing `RefreshSessionFamily.id` string-primary-key precedent — child agents
are referenced across process/session boundaries and need a stable opaque id.
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentRegistry(Base):
    """One spawned child agent instance and its ChildReceipt."""

    __tablename__ = "agent_registry"

    child_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_agent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_registry.child_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    agent_path: Mapped[str] = mapped_column(String(500), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    tools_allowlist: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    sandbox_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="read-only")
    task_brief: Mapped[str] = mapped_column(Text, nullable=False)
    budget: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="REQUESTED", index=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifacts: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<AgentRegistry(child_id={self.child_id!r}, role={self.role!r}, status={self.status!r})>"
```

Create `backend/app/models/hitl_proposal.py`:

```python
"""HITL (human-in-the-loop) approval proposal for sensitive agent actions.

`asset_id` for device-oriented proposals lives inside `action_payload` (JSON),
not as a dedicated foreign key — this keeps this table independent of the
CMDB subsystem (see docs/AGENT_ARCHITECTURE.md assumption A7).
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class HitlProposal(Base):
    """One write-action proposal awaiting human approval."""

    __tablename__ = "hitl_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposed_by_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_registry.child_id", ondelete="SET NULL"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    action_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"<HitlProposal(id={self.id}, action_type={self.action_type!r}, status={self.status!r})>"
```

Create `backend/app/models/agent_trace_event.py`:

```python
"""Append-only observability trace events for the agent runtime.

`agent_id`/`parent_agent_id` are plain strings, not foreign keys to
`agent_registry.child_id` — the root agent (which is not a spawned child and
has no registry row) also emits trace events under its own synthetic id.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentTraceEvent(Base):
    """One observability span emitted by the agent loop, a tool call, or spawn lifecycle."""

    __tablename__ = "agent_trace_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    span_type: Mapped[str] = mapped_column(String(20), nullable=False)
    tool: Mapped[str | None] = mapped_column(String(100), nullable=True)
    control: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"<AgentTraceEvent(id={self.id}, span_type={self.span_type!r}, tool={self.tool!r})>"
```

- [ ] **Step 4: Register the new models for Alembic autogenerate**

Modify `backend/app/models/__init__.py` — replace the full file contents with:

```python
"""模型包初始化，导出所有 ORM 模型。

供 Alembic autogenerate 和其他模块导入使用。
"""

from app.models.agent_message import AgentMessage
from app.models.agent_registry import AgentRegistry
from app.models.agent_session import AgentSession
from app.models.agent_trace_event import AgentTraceEvent
from app.models.audit_log import AuditLog
from app.models.base import Base
from app.models.hitl_proposal import HitlProposal
from app.models.permission import Permission
from app.models.refresh_session import RefreshSession
from app.models.refresh_session_family import RefreshSessionFamily
from app.models.role import Role, role_permissions
from app.models.user import User, user_roles

__all__ = [
    "Base",
    "User",
    "user_roles",
    "Role",
    "role_permissions",
    "Permission",
    "AuditLog",
    "RefreshSession",
    "RefreshSessionFamily",
    "AgentSession",
    "AgentMessage",
    "AgentRegistry",
    "HitlProposal",
    "AgentTraceEvent",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_models.py -v`
Expected: `5 passed`

- [ ] **Step 6: Write the Alembic migration**

Create `backend/alembic/versions/2026_08_10_1500-e1a4c7d9f215_agent_core_runtime.py`:

```python
"""Add agent-runtime core tables: sessions, messages, registry, HITL, trace events.

Revision ID: e1a4c7d9f215
Revises: d9f2b3c5a104
Create Date: 2026-08-10 15:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "e1a4c7d9f215"
down_revision: str | None = "d9f2b3c5a104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_destructive_downgrade() -> None:
    """Require an explicit opt-in before removing application tables."""
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_sessions_user_id"), "agent_sessions", ["user_id"], unique=False
    )

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_messages_session_id"), "agent_messages", ["session_id"], unique=False
    )

    op.create_table(
        "agent_registry",
        sa.Column("child_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("parent_agent_id", sa.String(length=36), nullable=True),
        sa.Column("agent_path", sa.String(length=500), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("tools_allowlist", sa.JSON(), nullable=False),
        sa.Column("sandbox_mode", sa.String(length=20), nullable=False),
        sa.Column("task_brief", sa.Text(), nullable=False),
        sa.Column("budget", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_agent_id"], ["agent_registry.child_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("child_id"),
    )
    op.create_index(
        op.f("ix_agent_registry_session_id"), "agent_registry", ["session_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_registry_parent_agent_id"),
        "agent_registry",
        ["parent_agent_id"],
        unique=False,
    )
    op.create_index(op.f("ix_agent_registry_status"), "agent_registry", ["status"], unique=False)

    op.create_table(
        "hitl_proposals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("proposed_by_agent_id", sa.String(length=36), nullable=True),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("action_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["proposed_by_agent_id"], ["agent_registry.child_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_hitl_proposals_session_id"), "hitl_proposals", ["session_id"], unique=False
    )
    op.create_index(op.f("ix_hitl_proposals_status"), "hitl_proposals", ["status"], unique=False)

    op.create_table(
        "agent_trace_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("parent_agent_id", sa.String(length=36), nullable=True),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("span_type", sa.String(length=20), nullable=False),
        sa.Column("tool", sa.String(length=100), nullable=True),
        sa.Column("control", sa.String(length=20), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error_class", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_trace_events_trace_id"), "agent_trace_events", ["trace_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_trace_events_session_id"),
        "agent_trace_events",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_table("agent_trace_events")
    op.drop_table("hitl_proposals")
    op.drop_table("agent_registry")
    op.drop_table("agent_messages")
    op.drop_table("agent_sessions")
```

- [ ] **Step 7: Apply the migration against your local Postgres**

Run: `uv run alembic upgrade head`
Expected: last printed line is `Running upgrade d9f2b3c5a104 -> e1a4c7d9f215, Add agent-runtime core tables: sessions, messages, registry, HITL, trace events` and the command exits 0. This requires `DATABASE_URL` in `backend/.env` to point at a running local Postgres (same one the RBAC backend already uses).

- [ ] **Step 8: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 9: Commit**

```bash
git add backend/app/models/agent_session.py backend/app/models/agent_message.py backend/app/models/agent_registry.py backend/app/models/hitl_proposal.py backend/app/models/agent_trace_event.py backend/app/models/__init__.py backend/alembic/versions/2026_08_10_1500-e1a4c7d9f215_agent_core_runtime.py backend/tests/test_agent_models.py
git commit -m "$(cat <<'EOF'
新增 Agent 运行时核心数据模型

- 新增 5 张表:agent_sessions(会话)、agent_messages(逐条消息,
  assistant 行的 tool_calls 以 JSON 存请求、tool 行的 tool_call_id
  标注对应哪次调用)、agent_registry(子 Agent 注册表/ChildReceipt,
  child_id 用 UUID 字符串主键,参照现有 RefreshSessionFamily 的做法)、
  hitl_proposals(HITL 提案状态机)、agent_trace_events(可观测性
  追加日志)
- 对应 Alembic 迁移 e1a4c7d9f215,downgrade 复用现有 init 迁移的
  _require_destructive_downgrade() 防呆机制
- hitl_proposals.action_payload 里的 asset_id 用松引用(见
  docs/AGENT_ARCHITECTURE.md 假设 A7),不建到 CMDB 的外键,保持这张表
  和后续 CMDB 子系统解耦
- 这是运维 Agent 平台(T06)的地基,后续 CRUD/loop/spawn 编排都建在
  这几张表之上
EOF
)"
```

---

### Task 2: CRUD — AgentSession

**Files:**
- Create: `backend/app/crud/agent_session.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_agent_crud_session.py`

**Interfaces:**
- Consumes: `app.crud.base.CRUDBase`, `app.models.agent_session.AgentSession` (Task 1).
- Produces: `agent_session_crud: CRUDAgentSession` singleton with `get(db, id)`, `create(db, obj_data)`, `update(db, id, obj_data)` (inherited from `CRUDBase`), plus `list_for_user(db, user_id, *, skip=0, limit=20) -> tuple[list[AgentSession], int]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_crud_session.py`:

```python
"""CRUD tests for AgentSession."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_session import agent_session_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_create_and_get(db_session: AsyncSession, test_user: User) -> None:
    session = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "巡检", "status": "active"}
    )
    await db_session.commit()

    fetched = await agent_session_crud.get(db_session, session.id)
    assert fetched is not None
    assert fetched.title == "巡检"


async def test_list_for_user_orders_newest_first_and_counts(
    db_session: AsyncSession, test_user: User
) -> None:
    first = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "第一次会话", "status": "active"}
    )
    await db_session.flush()
    second = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "第二次会话", "status": "active"}
    )
    await db_session.commit()

    items, total = await agent_session_crud.list_for_user(db_session, test_user.id)

    assert total == 2
    assert [item.id for item in items] == [second.id, first.id]


async def test_list_for_user_excludes_other_users(
    db_session: AsyncSession, test_user: User, superuser: User
) -> None:
    await agent_session_crud.create(
        db_session, {"user_id": superuser.id, "title": "别人的会话", "status": "active"}
    )
    await db_session.commit()

    items, total = await agent_session_crud.list_for_user(db_session, test_user.id)

    assert total == 0
    assert items == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_crud_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.agent_session'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/agent_session.py`:

```python
"""CRUD operations for agent chat sessions."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.agent_session import AgentSession


class CRUDAgentSession(CRUDBase[AgentSession]):
    """Agent session persistence; generic get/create/update come from CRUDBase."""

    model = AgentSession

    async def list_for_user(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[AgentSession], int]:
        """Return one user's sessions newest-first with a total count."""
        count_stmt = select(func.count()).select_from(AgentSession).where(
            AgentSession.user_id == user_id
        )
        total = (await db.execute(count_stmt)).scalar_one()

        stmt = (
            select(AgentSession)
            .where(AgentSession.user_id == user_id)
            .order_by(AgentSession.id.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all()), total


agent_session_crud = CRUDAgentSession()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — replace full contents with:

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_session import agent_session_crud
from app.crud.audit_log import audit_log_crud
from app.crud.dashboard import dashboard_crud
from app.crud.permission import permission_crud
from app.crud.role import role_crud
from app.crud.user import user_crud

__all__ = [
    "agent_session_crud",
    "audit_log_crud",
    "dashboard_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_crud_session.py -v`
Expected: `3 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/crud/agent_session.py backend/app/crud/__init__.py backend/tests/test_agent_crud_session.py
git commit -m "$(cat <<'EOF'
新增 AgentSession 的 CRUD 层

- CRUDAgentSession 继承 CRUDBase,复用通用的 get/create/update,只加
  了 list_for_user() 按用户分页倒序列会话(带 total 计数)
- agent_sessions 表没有 is_deleted 字段,所以不需要覆盖 soft_delete
EOF
)"
```

---

### Task 3: CRUD — AgentMessage

**Files:**
- Create: `backend/app/crud/agent_message.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_agent_crud_message.py`

**Interfaces:**
- Consumes: `app.models.agent_message.AgentMessage` (Task 1).
- Produces: `agent_message_crud: CRUDAgentMessage` singleton with:
  - `append(db, *, session_id, role, content, tool_call_id=None, tool_calls=None) -> AgentMessage`
  - `list_for_session(db, session_id, *, limit=None) -> list[AgentMessage]` (oldest-first; when `limit` is given, returns only the most recent `limit` messages, still oldest-first)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_crud_message.py`:

```python
"""CRUD tests for AgentMessage (append-only transcript storage)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_message import agent_message_crud
from app.crud.agent_session import agent_session_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def test_append_and_list_preserves_order(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)

    await agent_message_crud.append(db_session, session_id=session_id, role="user", content="在吗")
    await agent_message_crud.append(
        db_session, session_id=session_id, role="assistant", content="在的"
    )
    await db_session.commit()

    messages = await agent_message_crud.list_for_session(db_session, session_id)

    assert [m.content for m in messages] == ["在吗", "在的"]
    assert [m.role for m in messages] == ["user", "assistant"]


async def test_append_stores_tool_calls_and_tool_call_id(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)

    await agent_message_crud.append(
        db_session,
        session_id=session_id,
        role="assistant",
        content="",
        tool_calls=[{"id": "call_1", "name": "kb_grep", "arguments": "{}"}],
    )
    await agent_message_crud.append(
        db_session,
        session_id=session_id,
        role="tool",
        content="没找到匹配",
        tool_call_id="call_1",
    )
    await db_session.commit()

    messages = await agent_message_crud.list_for_session(db_session, session_id)

    assert messages[0].tool_calls == [{"id": "call_1", "name": "kb_grep", "arguments": "{}"}]
    assert messages[1].tool_call_id == "call_1"


async def test_list_for_session_limit_keeps_most_recent(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    for i in range(5):
        await agent_message_crud.append(
            db_session, session_id=session_id, role="user", content=f"msg-{i}"
        )
    await db_session.commit()

    messages = await agent_message_crud.list_for_session(db_session, session_id, limit=2)

    assert [m.content for m in messages] == ["msg-3", "msg-4"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_crud_message.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.agent_message'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/agent_message.py`:

```python
"""CRUD operations for agent transcript messages.

This is intentionally not a `CRUDBase` subclass: messages are append-only (no
update, no soft-delete), so the generic base's machinery does not apply.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_message import AgentMessage


class CRUDAgentMessage:
    """Append-only transcript storage for one agent session."""

    model = AgentMessage

    async def append(
        self,
        db: AsyncSession,
        *,
        session_id: int,
        role: str,
        content: str,
        tool_call_id: str | None = None,
        tool_calls: list[dict[str, str]] | None = None,
    ) -> AgentMessage:
        """Append one message to a session's transcript and flush."""
        message = AgentMessage(
            session_id=session_id,
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            tool_calls=tool_calls,
        )
        db.add(message)
        await db.flush()
        return message

    async def list_for_session(
        self,
        db: AsyncSession,
        session_id: int,
        *,
        limit: int | None = None,
    ) -> list[AgentMessage]:
        """Return a session's messages oldest-first, optionally capped to the most recent `limit`."""
        stmt = (
            select(AgentMessage)
            .where(AgentMessage.session_id == session_id)
            .order_by(AgentMessage.id.asc())
        )
        result = await db.execute(stmt)
        messages = list(result.scalars().all())
        if limit is not None and len(messages) > limit:
            return messages[-limit:]
        return messages


agent_message_crud = CRUDAgentMessage()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — add the import and `__all__` entry (alphabetical, matching the existing style):

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_session import agent_session_crud
from app.crud.audit_log import audit_log_crud
from app.crud.dashboard import dashboard_crud
from app.crud.permission import permission_crud
from app.crud.role import role_crud
from app.crud.user import user_crud

__all__ = [
    "agent_message_crud",
    "agent_session_crud",
    "audit_log_crud",
    "dashboard_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_crud_message.py -v`
Expected: `3 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/crud/agent_message.py backend/app/crud/__init__.py backend/tests/test_agent_crud_message.py
git commit -m "$(cat <<'EOF'
新增 AgentMessage 的 CRUD 层

- CRUDAgentMessage 不继承 CRUDBase(消息是纯追加,没有 update/软删除
  语义),只提供 append() 和 list_for_session()
- list_for_session 的 limit 参数保留"最近 N 条但仍按时间正序排列"
  的语义,方便后面 build_model_history 直接拼进模型请求
EOF
)"
```

---

### Task 4: CRUD — AgentRegistry (ChildReceipt + lifecycle state machine)

**Files:**
- Create: `backend/app/crud/agent_registry.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_agent_crud_registry.py`

**Interfaces:**
- Consumes: `app.models.agent_registry.AgentRegistry` (Task 1).
- Produces:
  - `InvalidAgentStatusTransitionError(current: str, target: str)` exception
  - `agent_registry_crud: CRUDAgentRegistry` singleton with:
    - `get(db, child_id) -> AgentRegistry | None`
    - `create(db, *, session_id, parent_agent_id, agent_path, role, model, tools_allowlist, sandbox_mode, task_brief, budget) -> AgentRegistry` (status starts `"REQUESTED"`)
    - `transition_status(db, child_id, target_status, *, result_summary=None, artifacts=None) -> AgentRegistry` — raises `InvalidAgentStatusTransitionError` on an illegal transition
    - `close(db, child_id) -> AgentRegistry` — idempotent forced-detach, always succeeds from any non-`CLOSED` status
    - `list_active_children(db, session_id) -> list[AgentRegistry]`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_crud_registry.py`:

```python
"""CRUD tests for AgentRegistry — the ChildReceipt store and its state machine."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_registry import InvalidAgentStatusTransitionError, agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def _spawn(db_session: AsyncSession, session_id: int) -> str:
    child = await agent_registry_crud.create(
        db_session,
        session_id=session_id,
        parent_agent_id=None,
        agent_path="/root/kb_explorer",
        role="kb_explorer",
        model="local-chat",
        tools_allowlist=["kb_grep"],
        sandbox_mode="read-only",
        task_brief="找一下重启流程",
        budget={"max_steps": 5},
    )
    await db_session.commit()
    return child.child_id


async def test_create_starts_requested(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)

    fetched = await agent_registry_crud.get(db_session, child_id)
    assert fetched is not None
    assert fetched.status == "REQUESTED"


async def test_valid_transition_chain(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)

    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")
    await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")
    updated = await agent_registry_crud.transition_status(
        db_session, child_id, "COMPLETED", result_summary="找到了,在 SOP 第 3 章"
    )
    await db_session.commit()

    assert updated.status == "COMPLETED"
    assert updated.result_summary == "找到了,在 SOP 第 3 章"


async def test_illegal_transition_raises(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)

    with pytest.raises(InvalidAgentStatusTransitionError):
        await agent_registry_crud.transition_status(db_session, child_id, "COMPLETED")


async def test_close_is_idempotent_and_force_detaches_running(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")
    await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")

    first_close = await agent_registry_crud.close(db_session, child_id)
    assert first_close.status == "CLOSED"
    assert first_close.closed_at is not None

    second_close = await agent_registry_crud.close(db_session, child_id)
    assert second_close.status == "CLOSED"
    assert second_close.closed_at == first_close.closed_at


async def test_list_active_children_excludes_closed(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    still_running = await _spawn(db_session, session_id)
    closed_one = await _spawn(db_session, session_id)
    await agent_registry_crud.close(db_session, closed_one)
    await db_session.commit()

    active = await agent_registry_crud.list_active_children(db_session, session_id)

    assert [c.child_id for c in active] == [still_running]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_crud_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.agent_registry'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/agent_registry.py`:

```python
"""CRUD operations for the child-agent registry (ChildReceipt store).

Implements the lifecycle state machine from docs/guide.md §7.3:
REQUESTED -> SPAWNING -> RUNNING -> COMPLETED|FAILED|CANCELLED -> CLOSED.
`close()` is the one operation allowed from any non-terminal status — it is
the forced-detach escape valve so a hung child can always free its slot.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_registry import AgentRegistry

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "REQUESTED": {"SPAWNING", "FAILED", "CANCELLED"},
    "SPAWNING": {"RUNNING", "FAILED", "CANCELLED"},
    "RUNNING": {"COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": {"CLOSED"},
    "FAILED": {"CLOSED"},
    "CANCELLED": {"CLOSED"},
    "CLOSED": set(),
}


class InvalidAgentStatusTransitionError(ValueError):
    """Raised when a status transition violates the agent lifecycle state machine."""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"cannot transition agent status from {current!r} to {target!r}")


class CRUDAgentRegistry:
    """Child-agent registry persistence and lifecycle transitions."""

    model = AgentRegistry

    async def get(self, db: AsyncSession, child_id: str) -> AgentRegistry | None:
        """Return one registry row by child_id."""
        stmt = select(AgentRegistry).where(AgentRegistry.child_id == child_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        db: AsyncSession,
        *,
        session_id: int,
        parent_agent_id: str | None,
        agent_path: str,
        role: str,
        model: str,
        tools_allowlist: list[str],
        sandbox_mode: str,
        task_brief: str,
        budget: dict[str, object],
    ) -> AgentRegistry:
        """Register a newly spawned child agent in REQUESTED status and flush."""
        registry = AgentRegistry(
            session_id=session_id,
            parent_agent_id=parent_agent_id,
            agent_path=agent_path,
            role=role,
            model=model,
            tools_allowlist=tools_allowlist,
            sandbox_mode=sandbox_mode,
            task_brief=task_brief,
            budget=budget,
            status="REQUESTED",
        )
        db.add(registry)
        await db.flush()
        return registry

    async def transition_status(
        self,
        db: AsyncSession,
        child_id: str,
        target_status: str,
        *,
        result_summary: str | None = None,
        artifacts: list[str] | None = None,
    ) -> AgentRegistry:
        """Move a child agent to `target_status`, enforcing the lifecycle state machine."""
        registry = await self.get(db, child_id)
        if registry is None:
            raise ValueError(f"agent registry {child_id!r} not found")

        allowed = _ALLOWED_TRANSITIONS.get(registry.status, set())
        if target_status not in allowed:
            raise InvalidAgentStatusTransitionError(registry.status, target_status)

        registry.status = target_status
        if result_summary is not None:
            registry.result_summary = result_summary
        if artifacts is not None:
            registry.artifacts = artifacts
        if target_status == "CLOSED":
            registry.closed_at = datetime.now(UTC)

        await db.flush()
        return registry

    async def close(self, db: AsyncSession, child_id: str) -> AgentRegistry:
        """Idempotently close a child agent, bypassing the normal transition table."""
        registry = await self.get(db, child_id)
        if registry is None:
            raise ValueError(f"agent registry {child_id!r} not found")
        if registry.status != "CLOSED":
            registry.status = "CLOSED"
            registry.closed_at = datetime.now(UTC)
            await db.flush()
        return registry

    async def list_active_children(self, db: AsyncSession, session_id: int) -> list[AgentRegistry]:
        """Return every child in this session not yet CLOSED, oldest-first."""
        stmt = (
            select(AgentRegistry)
            .where(AgentRegistry.session_id == session_id, AgentRegistry.status != "CLOSED")
            .order_by(AgentRegistry.created_at.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


agent_registry_crud = CRUDAgentRegistry()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — add `agent_registry_crud` (alphabetical order):

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.audit_log import audit_log_crud
from app.crud.dashboard import dashboard_crud
from app.crud.permission import permission_crud
from app.crud.role import role_crud
from app.crud.user import user_crud

__all__ = [
    "agent_message_crud",
    "agent_registry_crud",
    "agent_session_crud",
    "audit_log_crud",
    "dashboard_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_crud_registry.py -v`
Expected: `5 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/crud/agent_registry.py backend/app/crud/__init__.py backend/tests/test_agent_crud_registry.py
git commit -m "$(cat <<'EOF'
新增 AgentRegistry 的 CRUD 层与生命周期状态机

- 按 docs/guide.md §7.3 的状态机实现 transition_status():
  REQUESTED->SPAWNING->RUNNING->COMPLETED|FAILED|CANCELLED->CLOSED,
  非法迁移抛 InvalidAgentStatusTransitionError
- close() 单独实现为幂等的强制 detach,不走 transition_status 的
  合法性校验——这是手册里"超时强制释放槽位"的逃生舱,必须在任何
  非 CLOSED 状态下都能成功
- list_active_children() 供后续 spawn 编排器做 wait_all/级联关闭时
  查询还没关闭的子 Agent
EOF
)"
```

---

### Task 5: CRUD — HitlProposal (state machine)

**Files:**
- Create: `backend/app/crud/hitl_proposal.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_agent_crud_hitl.py`

**Interfaces:**
- Consumes: `app.models.hitl_proposal.HitlProposal` (Task 1).
- Produces:
  - `InvalidHitlTransitionError(current: str, target: str)` exception
  - `hitl_proposal_crud: CRUDHitlProposal` singleton with:
    - `get(db, proposal_id) -> HitlProposal | None`
    - `create(db, *, session_id, proposed_by_agent_id, action_type, action_payload) -> HitlProposal` (status starts `"PENDING"`)
    - `decide(db, proposal_id, *, approve: bool, reviewed_by_user_id: int) -> HitlProposal` — only `PENDING` may be decided
    - `mark_executed(db, proposal_id) -> HitlProposal` — only `APPROVED` may become `EXECUTED`, and only once

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_crud_hitl.py`:

```python
"""CRUD tests for HitlProposal — the PENDING/APPROVED/REJECTED/EXECUTED state machine."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_session import agent_session_crud
from app.crud.hitl_proposal import InvalidHitlTransitionError, hitl_proposal_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def test_create_starts_pending(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)

    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "SW-12 离线"},
    )
    await db_session.commit()

    assert proposal.status == "PENDING"


async def test_approve_sets_reviewer_and_timestamp(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={},
    )
    await db_session.flush()

    approved = await hitl_proposal_crud.decide(
        db_session, proposal.id, approve=True, reviewed_by_user_id=test_user.id
    )
    await db_session.commit()

    assert approved.status == "APPROVED"
    assert approved.reviewed_by_user_id == test_user.id
    assert approved.reviewed_at is not None


async def test_cannot_decide_twice(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={},
    )
    await db_session.flush()
    await hitl_proposal_crud.decide(
        db_session, proposal.id, approve=True, reviewed_by_user_id=test_user.id
    )

    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.decide(
            db_session, proposal.id, approve=False, reviewed_by_user_id=test_user.id
        )


async def test_mark_executed_requires_approved_first(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={},
    )
    await db_session.flush()

    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.mark_executed(db_session, proposal.id)

    await hitl_proposal_crud.decide(
        db_session, proposal.id, approve=True, reviewed_by_user_id=test_user.id
    )
    executed = await hitl_proposal_crud.mark_executed(db_session, proposal.id)
    await db_session.commit()

    assert executed.status == "EXECUTED"
    assert executed.executed_at is not None

    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.mark_executed(db_session, proposal.id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_crud_hitl.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.hitl_proposal'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/hitl_proposal.py`:

```python
"""CRUD operations for HITL (human-in-the-loop) approval proposals.

State machine (docs/guide.md §5.3): PENDING -[approve]-> APPROVED -[resume]->
EXECUTED (exactly once); PENDING -[reject]-> REJECTED. Only PENDING may be
decided; only APPROVED may become EXECUTED.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hitl_proposal import HitlProposal


class InvalidHitlTransitionError(ValueError):
    """Raised when a HITL proposal transition violates the approval state machine."""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"cannot transition HITL proposal from {current!r} to {target!r}")


class CRUDHitlProposal:
    """HITL proposal persistence and state-machine transitions."""

    model = HitlProposal

    async def get(self, db: AsyncSession, proposal_id: int) -> HitlProposal | None:
        """Return one proposal by id."""
        stmt = select(HitlProposal).where(HitlProposal.id == proposal_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        db: AsyncSession,
        *,
        session_id: int,
        proposed_by_agent_id: str | None,
        action_type: str,
        action_payload: dict[str, object],
    ) -> HitlProposal:
        """Create a new proposal in PENDING status and flush."""
        proposal = HitlProposal(
            session_id=session_id,
            proposed_by_agent_id=proposed_by_agent_id,
            action_type=action_type,
            action_payload=action_payload,
            status="PENDING",
        )
        db.add(proposal)
        await db.flush()
        return proposal

    async def decide(
        self,
        db: AsyncSession,
        proposal_id: int,
        *,
        approve: bool,
        reviewed_by_user_id: int,
    ) -> HitlProposal:
        """Move a PENDING proposal to APPROVED or REJECTED. Only PENDING may be decided."""
        proposal = await self.get(db, proposal_id)
        if proposal is None:
            raise ValueError(f"HITL proposal {proposal_id} not found")
        target = "APPROVED" if approve else "REJECTED"
        if proposal.status != "PENDING":
            raise InvalidHitlTransitionError(proposal.status, target)

        proposal.status = target
        proposal.reviewed_by_user_id = reviewed_by_user_id
        proposal.reviewed_at = datetime.now(UTC)
        await db.flush()
        return proposal

    async def mark_executed(self, db: AsyncSession, proposal_id: int) -> HitlProposal:
        """Move an APPROVED proposal to EXECUTED exactly once."""
        proposal = await self.get(db, proposal_id)
        if proposal is None:
            raise ValueError(f"HITL proposal {proposal_id} not found")
        if proposal.status != "APPROVED":
            raise InvalidHitlTransitionError(proposal.status, "EXECUTED")

        proposal.status = "EXECUTED"
        proposal.executed_at = datetime.now(UTC)
        await db.flush()
        return proposal


hitl_proposal_crud = CRUDHitlProposal()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — add `hitl_proposal_crud` (alphabetical order):

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.audit_log import audit_log_crud
from app.crud.dashboard import dashboard_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.crud.permission import permission_crud
from app.crud.role import role_crud
from app.crud.user import user_crud

__all__ = [
    "agent_message_crud",
    "agent_registry_crud",
    "agent_session_crud",
    "audit_log_crud",
    "dashboard_crud",
    "hitl_proposal_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_crud_hitl.py -v`
Expected: `4 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/crud/hitl_proposal.py backend/app/crud/__init__.py backend/tests/test_agent_crud_hitl.py
git commit -m "$(cat <<'EOF'
新增 HitlProposal 的 CRUD 层与审批状态机

- 按 docs/guide.md §5.3 硬规则实现:只有 PENDING 能被 decide(),
  只有 APPROVED 能 mark_executed() 且只能成功一次,非法迁移抛
  InvalidHitlTransitionError
- decide()/mark_executed() 都是这张表状态转移的唯一入口,不提供
  通用的 update() 绕过状态机
EOF
)"
```

---

### Task 6: CRUD — AgentTraceEvent (append-only observability log)

**Files:**
- Create: `backend/app/crud/agent_trace_event.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_agent_crud_trace.py`

**Interfaces:**
- Consumes: `app.models.agent_trace_event.AgentTraceEvent` (Task 1).
- Produces: `agent_trace_event_crud: CRUDAgentTraceEvent` singleton with:
  - `record(db, *, trace_id, session_id, agent_id, parent_agent_id, step, span_type, tool=None, control=None, cost_usd=0.0, latency_ms=0, error_class=None) -> AgentTraceEvent`
  - `list_for_trace(db, trace_id) -> list[AgentTraceEvent]` (ordered by `step` ascending)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_crud_trace.py`:

```python
"""CRUD tests for AgentTraceEvent (append-only observability log)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def test_record_and_list_ordered_by_step(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)

    await agent_trace_event_crud.record(
        db_session,
        trace_id="trace-1",
        session_id=session_id,
        agent_id="root",
        parent_agent_id=None,
        step=2,
        span_type="tool",
        tool="kb_grep",
        control="ok",
        cost_usd=0.001,
        latency_ms=80,
    )
    await agent_trace_event_crud.record(
        db_session,
        trace_id="trace-1",
        session_id=session_id,
        agent_id="root",
        parent_agent_id=None,
        step=1,
        span_type="generation",
    )
    await db_session.commit()

    events = await agent_trace_event_crud.list_for_trace(db_session, "trace-1")

    assert [e.step for e in events] == [1, 2]
    assert events[1].tool == "kb_grep"


async def test_list_for_trace_excludes_other_traces(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await agent_trace_event_crud.record(
        db_session,
        trace_id="trace-a",
        session_id=session_id,
        agent_id="root",
        parent_agent_id=None,
        step=1,
        span_type="generation",
    )
    await agent_trace_event_crud.record(
        db_session,
        trace_id="trace-b",
        session_id=session_id,
        agent_id="root",
        parent_agent_id=None,
        step=1,
        span_type="generation",
    )
    await db_session.commit()

    events = await agent_trace_event_crud.list_for_trace(db_session, "trace-a")

    assert len(events) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_crud_trace.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.agent_trace_event'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/agent_trace_event.py`:

```python
"""CRUD operations for agent observability trace events (append-only)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_trace_event import AgentTraceEvent


class CRUDAgentTraceEvent:
    """Append-only trace/span storage."""

    model = AgentTraceEvent

    async def record(
        self,
        db: AsyncSession,
        *,
        trace_id: str,
        session_id: int,
        agent_id: str,
        parent_agent_id: str | None,
        step: int,
        span_type: str,
        tool: str | None = None,
        control: str | None = None,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        error_class: str | None = None,
    ) -> AgentTraceEvent:
        """Append one trace event and flush."""
        event = AgentTraceEvent(
            trace_id=trace_id,
            session_id=session_id,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            step=step,
            span_type=span_type,
            tool=tool,
            control=control,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            error_class=error_class,
        )
        db.add(event)
        await db.flush()
        return event

    async def list_for_trace(self, db: AsyncSession, trace_id: str) -> list[AgentTraceEvent]:
        """Return every span for one trace, ordered by step."""
        stmt = (
            select(AgentTraceEvent)
            .where(AgentTraceEvent.trace_id == trace_id)
            .order_by(AgentTraceEvent.step.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


agent_trace_event_crud = CRUDAgentTraceEvent()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — add `agent_trace_event_crud` (alphabetical order):

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.crud.audit_log import audit_log_crud
from app.crud.dashboard import dashboard_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.crud.permission import permission_crud
from app.crud.role import role_crud
from app.crud.user import user_crud

__all__ = [
    "agent_message_crud",
    "agent_registry_crud",
    "agent_session_crud",
    "agent_trace_event_crud",
    "audit_log_crud",
    "dashboard_crud",
    "hitl_proposal_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_crud_trace.py -v`
Expected: `2 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/crud/agent_trace_event.py backend/app/crud/__init__.py backend/tests/test_agent_crud_trace.py
git commit -m "$(cat <<'EOF'
新增 AgentTraceEvent 的 CRUD 层

- 同 AgentMessage,append-only,不继承 CRUDBase
- record()/list_for_trace() 对应 docs/guide.md §8.2-8.3 的可观测性
  埋点需求,字段跟手册里的日志字段建议一一对应
EOF
)"
```

---

### Task 7: `app/core/llm.py` — model registry and unified `chat()`

**Files:**
- Modify: `backend/app/core/config.py:38-43` (add the LLM settings block right after the database settings)
- Modify: `backend/.env.example:11-12` (add example LLM env vars after `DB_MAX_OVERFLOW`)
- Create: `backend/app/core/llm.py`
- Test: `backend/tests/test_agent_llm.py`

**Interfaces:**
- Consumes: `app.core.config.settings` (new `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_CHAT_MODEL`, `LLM_EMBEDDING_MODEL` fields).
- Produces (used by Task 10's `loop.py` and every later knowledge/tool task):
  - `@dataclass ToolCall(id: str, name: str, arguments: str)`
  - `@dataclass ChatMessage(role: str, content: str, tool_call_id: str | None = None, tool_calls: list[ToolCall] | None = None)`
  - `@dataclass ChatResult(content: str | None, tool_calls: list[ToolCall], finish_reason: str, prompt_tokens: int, completion_tokens: int)`
  - `class LlmRequestError(RuntimeError)`
  - `MODELS: dict[str, ModelConfig]` registry (`"local-chat"`, `"local-embedding"` entries)
  - `async def chat(model_key: str, messages: list[ChatMessage], *, tools: list[dict[str, Any]] | None = None, client: httpx.AsyncClient | None = None) -> ChatResult`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_llm.py`:

```python
"""Tests for the unified LLM client (app.core.llm)."""

import json
from typing import Any

import httpx
import pytest

from app.core.llm import ChatMessage, LlmRequestError, ToolCall, chat

pytestmark = pytest.mark.asyncio


def _fake_transport(json_body: dict[str, object], status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body)

    return httpx.MockTransport(handler)


async def test_chat_returns_parsed_text_result() -> None:
    transport = _fake_transport(
        {
            "choices": [
                {"message": {"content": "你好", "tool_calls": []}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        result = await chat(
            "local-chat",
            [ChatMessage(role="user", content="在吗")],
            client=fake_client,
        )

    assert result.content == "你好"
    assert result.tool_calls == []
    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 3


async def test_chat_parses_tool_calls() -> None:
    transport = _fake_transport(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "kb_grep", "arguments": '{"pattern": "重启"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5},
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        result = await chat(
            "local-chat",
            [ChatMessage(role="user", content="帮我查一下重启流程")],
            client=fake_client,
        )

    assert result.content is None
    assert result.tool_calls == [ToolCall(id="call_1", name="kb_grep", arguments='{"pattern": "重启"}')]


async def test_chat_replays_tool_calls_and_tool_call_id_in_request_body() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["parsed"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "好的", "tool_calls": []}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        await chat(
            "local-chat",
            [
                ChatMessage(role="user", content="查一下"),
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id="call_1", name="kb_grep", arguments="{}")],
                ),
                ChatMessage(role="tool", content="没找到", tool_call_id="call_1"),
            ],
            client=fake_client,
        )

    sent_messages = captured["parsed"]["messages"]
    assert sent_messages[1]["tool_calls"][0]["function"]["name"] == "kb_grep"
    assert sent_messages[2]["tool_call_id"] == "call_1"


async def test_chat_raises_on_non_200() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        with pytest.raises(LlmRequestError):
            await chat("local-chat", [ChatMessage(role="user", content="hi")], client=fake_client)


async def test_chat_rejects_unknown_model_key() -> None:
    with pytest.raises(LlmRequestError):
        await chat("does-not-exist", [ChatMessage(role="user", content="hi")])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.llm'`.

- [ ] **Step 3: Add LLM settings to config.py**

Modify `backend/app/core/config.py` — insert this block immediately after the `DB_MAX_OVERFLOW` line (currently line 42) and before the `# JWT / 会话` comment (currently line 44):

```python
    # 数据库
    DATABASE_URL: str = DEFAULT_DATABASE_URL
    MIGRATION_DATABASE_URL: SecretStr | None = None
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=100)
    DB_MAX_OVERFLOW: int = Field(default=5, ge=0, le=100)

    # LLM(本地 llama.cpp OpenAI 兼容接口)
    LLM_BASE_URL: str = "http://127.0.0.1:8080/v1"
    LLM_API_KEY: SecretStr | None = None
    LLM_CHAT_MODEL: str = "local-chat"
    LLM_EMBEDDING_MODEL: str = "Qwen3-Embedding-0.6B"

    # JWT / 会话
```

Then add a matching `@property` accessor next to the existing `secret_key` property (after line 174's closing of `secret_key`, before `migration_database_url`):

```python
    @property
    def llm_api_key(self) -> str:
        """Return the LLM provider API key, or an empty string when none is configured."""
        if self.LLM_API_KEY is None:
            return ""
        return self.LLM_API_KEY.get_secret_value()
```

- [ ] **Step 4: Document the new env vars**

Modify `backend/.env.example` — insert this block right after the `DB_MAX_OVERFLOW=5` line (currently line 11) and before the blank line preceding the `# 在开发中` comment:

```ini
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=5

# 本地 llama.cpp OpenAI 兼容接口(chat + embedding)
LLM_BASE_URL=http://127.0.0.1:8080/v1
# LLM_API_KEY=
LLM_CHAT_MODEL=local-chat
LLM_EMBEDDING_MODEL=Qwen3-Embedding-0.6B
```

- [ ] **Step 5: Implement `app/core/llm.py`**

Create `backend/app/core/llm.py`:

```python
"""Unified LLM client.

Every call into a model provider goes through `chat()`. New models are
registered by adding one entry to `MODELS` — nothing else in the codebase
should construct an HTTP client to a model provider directly.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """One entry in the model registry."""

    name: str
    base_url: str
    api_key: str
    request_model: str
    timeout_seconds: float = 60.0


MODELS: dict[str, ModelConfig] = {
    "local-chat": ModelConfig(
        name="local-chat",
        base_url=settings.LLM_BASE_URL,
        api_key=settings.llm_api_key,
        request_model=settings.LLM_CHAT_MODEL,
    ),
    "local-embedding": ModelConfig(
        name="local-embedding",
        base_url=settings.LLM_BASE_URL,
        api_key=settings.llm_api_key,
        request_model=settings.LLM_EMBEDDING_MODEL,
    ),
}


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call requested by the model."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One OpenAI-compatible chat message."""

    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None


@dataclass(frozen=True, slots=True)
class ChatResult:
    """The assistant turn returned by `chat()`."""

    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int


class LlmRequestError(RuntimeError):
    """Raised when the model provider returns a non-2xx response or a malformed body."""


def _message_to_payload(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in message.tool_calls
        ]
    return payload


def _build_client(config: ModelConfig) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    return httpx.AsyncClient(base_url=config.base_url, headers=headers, timeout=config.timeout_seconds)


async def chat(
    model_key: str,
    messages: list[ChatMessage],
    *,
    tools: list[dict[str, Any]] | None = None,
    client: httpx.AsyncClient | None = None,
) -> ChatResult:
    """Send one OpenAI-compatible chat completion request and return the assistant turn.

    `client` is injectable for tests (pass an `httpx.AsyncClient(transport=httpx.MockTransport(...))`);
    production callers omit it and a short-lived client is created per call.
    """
    config = MODELS.get(model_key)
    if config is None:
        raise LlmRequestError(f"unknown model key {model_key!r}; register it in MODELS first")

    payload: dict[str, Any] = {
        "model": config.request_model,
        "messages": [_message_to_payload(m) for m in messages],
    }
    if tools:
        payload["tools"] = tools

    owns_client = client is None
    http_client = client or _build_client(config)
    try:
        response = await http_client.post("/chat/completions", json=payload)
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code != 200:
        raise LlmRequestError(
            f"model {model_key!r} returned HTTP {response.status_code}: {response.text}"
        )

    body = response.json()
    try:
        choice = body["choices"][0]
        message = choice["message"]
        raw_tool_calls = message.get("tool_calls") or []
        tool_calls = [
            ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=tc["function"]["arguments"])
            for tc in raw_tool_calls
        ]
        usage = body.get("usage", {})
        return ChatResult(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice["finish_reason"],
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
    except (KeyError, IndexError) as exc:
        raise LlmRequestError(f"malformed response body from model {model_key!r}: {body}") from exc
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_llm.py -v`
Expected: `5 passed`

- [ ] **Step 7: Run the full existing suite to confirm the config change didn't break anything**

Run: `uv run pytest -v`
Expected: all previously-passing tests still pass (config.py's `extra="forbid"` means a typo'd env var name would now break every test at import time — this step catches that).

- [ ] **Step 8: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 9: Commit**

```bash
git add backend/app/core/config.py backend/.env.example backend/app/core/llm.py backend/tests/test_agent_llm.py
git commit -m "$(cat <<'EOF'
新增统一 LLM 客户端 app/core/llm.py

- 按 CLAUDE.md 的约定,新增模型只在 MODELS 登记表里加一行;当前登记
  本地 llama.cpp 的 chat 和 embedding(Qwen3-Embedding-0.6B)两个
  OpenAI 兼容端点
- chat() 的 client 参数可注入,测试用 httpx.MockTransport 覆盖正常
  返回、tool_calls 解析、请求体里正确回放 assistant 的 tool_calls 和
  tool 消息的 tool_call_id、非 200 响应、未知 model_key 五种情况,
  不需要真的起一个 llama.cpp 服务
- config.py 新增 LLM_BASE_URL/LLM_API_KEY/LLM_CHAT_MODEL/
  LLM_EMBEDDING_MODEL 四个配置项,同步更新 .env.example
EOF
)"
```

---

### Task 8: `app/agent/budget.py` — per-run budget tracking

**Files:**
- Create: `backend/app/agent/__init__.py`
- Create: `backend/app/agent/budget.py`
- Test: `backend/tests/test_agent_budget.py`

**Interfaces:**
- Consumes: nothing (pure logic, no DB/IO).
- Produces (used by Task 10's `loop.py`):
  - `class BudgetExceededError(RuntimeError)` with `limit_name: str`, `limit: float`, `used: float` attributes
  - `@dataclass class Budget(max_steps: int = 20, max_cost_usd: float = 1.0)` with mutable `steps_used: int`, `cost_used_usd: float` and method `record_step(cost_usd: float = 0.0) -> None`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_budget.py`:

```python
"""Tests for the per-run budget tracker (app.agent.budget)."""

import pytest

from app.agent.budget import Budget, BudgetExceededError


def test_record_step_accumulates_usage() -> None:
    budget = Budget(max_steps=5, max_cost_usd=1.0)

    budget.record_step(cost_usd=0.1)
    budget.record_step(cost_usd=0.2)

    assert budget.steps_used == 2
    assert budget.cost_used_usd == pytest.approx(0.3)


def test_record_step_raises_when_max_steps_exceeded() -> None:
    budget = Budget(max_steps=1, max_cost_usd=100.0)
    budget.record_step()

    with pytest.raises(BudgetExceededError) as exc_info:
        budget.record_step()

    assert exc_info.value.limit_name == "max_steps"


def test_record_step_raises_when_max_cost_exceeded() -> None:
    budget = Budget(max_steps=100, max_cost_usd=0.5)

    with pytest.raises(BudgetExceededError) as exc_info:
        budget.record_step(cost_usd=0.6)

    assert exc_info.value.limit_name == "max_cost_usd"


def test_default_limits_match_docs_agent_architecture() -> None:
    budget = Budget()

    assert budget.max_steps == 20
    assert budget.max_cost_usd == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_budget.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent'`.

- [ ] **Step 3: Implement the module**

Create `backend/app/agent/__init__.py`:

```python
"""Agent runtime: loop, session helpers, and budget tracking.

Peer package to app/api, app/crud, app/models, app/services — see
docs/AGENT_ARCHITECTURE.md §2 for the layering rule (this package may call
app/crud, never bypass it with raw SQL).
"""
```

Create `backend/app/agent/budget.py`:

```python
"""Per-run budget tracking and enforcement (docs/guide.md §9.1).

The loop must stop, not retry, when a limit is exceeded — `record_step`
raises rather than silently clamping so the caller can only proceed by
catching the error, never by accident.
"""

from dataclasses import dataclass, field


class BudgetExceededError(RuntimeError):
    """Raised when a budget limit is exceeded; the loop must stop, not retry."""

    def __init__(self, limit_name: str, limit: float, used: float) -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.used = used
        super().__init__(f"budget exceeded: {limit_name} used {used} > limit {limit}")


@dataclass
class Budget:
    """Mutable per-run budget tracker; defaults match docs/AGENT_ARCHITECTURE.md §10."""

    max_steps: int = 20
    max_cost_usd: float = 1.0
    steps_used: int = field(default=0, init=False)
    cost_used_usd: float = field(default=0.0, init=False)

    def record_step(self, cost_usd: float = 0.0) -> None:
        """Record one loop iteration's cost, raising if any limit is now exceeded."""
        self.steps_used += 1
        self.cost_used_usd += cost_usd
        if self.steps_used > self.max_steps:
            raise BudgetExceededError("max_steps", self.max_steps, self.steps_used)
        if self.cost_used_usd > self.max_cost_usd:
            raise BudgetExceededError("max_cost_usd", self.max_cost_usd, self.cost_used_usd)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_budget.py -v`
Expected: `4 passed`

- [ ] **Step 5: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agent/__init__.py backend/app/agent/budget.py backend/tests/test_agent_budget.py
git commit -m "$(cat <<'EOF'
新增 app/agent/ 包与预算跟踪器 Budget

- 新建 app/agent/ 顶层包,跟 app/api、app/crud、app/models、
  app/services 平级,是运维 Agent 平台的内核所在(见
  docs/AGENT_ARCHITECTURE.md 总体架构)
- Budget.record_step() 累计步数和花费,超出 max_steps/max_cost_usd
  任一项就抛 BudgetExceededError,默认值 20 步/1 美元对应架构文档
  §10 的预算配置表
EOF
)"
```

---

### Task 9: `app/agent/session.py` — transcript helpers

**Files:**
- Create: `backend/app/agent/session.py`
- Test: `backend/tests/test_agent_session.py`

**Interfaces:**
- Consumes: `app.crud.agent_message.agent_message_crud`, `app.crud.agent_session.agent_session_crud` (Tasks 2, 3), `app.core.llm.ChatMessage`, `app.core.llm.ToolCall` (Task 7).
- Produces (used by Task 10's `loop.py`):
  - `async def build_model_history(db, session_id: int, *, max_messages: int = 40) -> list[ChatMessage]`
  - `async def append_user_message(db, session_id: int, content: str) -> AgentMessage`
  - `async def append_assistant_message(db, session_id: int, content: str, *, tool_calls: list[ToolCall] | None = None) -> AgentMessage`
  - `async def append_tool_result(db, session_id: int, tool_call_id: str, content: str) -> AgentMessage`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_session.py`:

```python
"""Tests for app.agent.session — transcript helpers built on the CRUD layer."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.session import (
    append_assistant_message,
    append_tool_result,
    append_user_message,
    build_model_history,
)
from app.core.llm import ToolCall
from app.crud.agent_session import agent_session_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def test_build_model_history_round_trips_plain_messages(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "网段 10.0.0.0/24 有谁掉线了")
    await append_assistant_message(db_session, session_id, "让我查一下")
    await db_session.commit()

    history = await build_model_history(db_session, session_id)

    assert [m.role for m in history] == ["user", "assistant"]
    assert history[0].content == "网段 10.0.0.0/24 有谁掉线了"


async def test_build_model_history_round_trips_tool_calls(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "查一下")
    await append_assistant_message(
        db_session,
        session_id,
        "",
        tool_calls=[ToolCall(id="call_1", name="query_monitor_status", arguments="{}")],
    )
    await append_tool_result(db_session, session_id, "call_1", "10.0.0.5 离线")
    await db_session.commit()

    history = await build_model_history(db_session, session_id)

    assert history[1].tool_calls == [ToolCall(id="call_1", name="query_monitor_status", arguments="{}")]
    assert history[2].role == "tool"
    assert history[2].tool_call_id == "call_1"
    assert history[2].content == "10.0.0.5 离线"


async def test_build_model_history_respects_max_messages(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    for i in range(6):
        await append_user_message(db_session, session_id, f"msg-{i}")
    await db_session.commit()

    history = await build_model_history(db_session, session_id, max_messages=2)

    assert [m.content for m in history] == ["msg-4", "msg-5"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent.session'`.

- [ ] **Step 3: Implement the module**

Create `backend/app/agent/session.py`:

```python
"""Transcript helpers built on top of the AgentMessage CRUD layer.

Deliberately no compaction/summarization here yet — `build_model_history`
returns a bounded recent window only. Summarizing older turns is deferred to
a later plan (see docs/AGENT_ARCHITECTURE.md §8 and guide.md §6.3); adding it
now would be speculative for a subsystem nothing yet exercises end-to-end.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import ChatMessage, ToolCall
from app.crud.agent_message import agent_message_crud
from app.models.agent_message import AgentMessage


async def build_model_history(
    db: AsyncSession,
    session_id: int,
    *,
    max_messages: int = 40,
) -> list[ChatMessage]:
    """Return this session's most recent messages as model-ready ChatMessages."""
    rows = await agent_message_crud.list_for_session(db, session_id, limit=max_messages)
    history: list[ChatMessage] = []
    for row in rows:
        tool_calls: list[ToolCall] | None = None
        if row.tool_calls:
            tool_calls = [
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                for tc in row.tool_calls
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


async def append_user_message(db: AsyncSession, session_id: int, content: str) -> AgentMessage:
    """Append one user turn."""
    return await agent_message_crud.append(db, session_id=session_id, role="user", content=content)


async def append_assistant_message(
    db: AsyncSession,
    session_id: int,
    content: str,
    *,
    tool_calls: list[ToolCall] | None = None,
) -> AgentMessage:
    """Append one assistant turn, optionally carrying the tool calls it requested."""
    serialized = None
    if tool_calls:
        serialized = [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls]
    return await agent_message_crud.append(
        db, session_id=session_id, role="assistant", content=content, tool_calls=serialized
    )


async def append_tool_result(
    db: AsyncSession,
    session_id: int,
    tool_call_id: str,
    content: str,
) -> AgentMessage:
    """Append one tool-result turn, correlated back to the call it answers."""
    return await agent_message_crud.append(
        db, session_id=session_id, role="tool", content=content, tool_call_id=tool_call_id
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_session.py -v`
Expected: `3 passed`

- [ ] **Step 5: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agent/session.py backend/tests/test_agent_session.py
git commit -m "$(cat <<'EOF'
新增 app/agent/session.py 会话记录辅助函数

- build_model_history() 把 AgentMessage 行还原成 app.core.llm 的
  ChatMessage 列表(含 tool_calls/tool_call_id 的正确往返),供
  loop.py 每一步喂给 chat()
- append_user_message/append_assistant_message/append_tool_result 是
  对 CRUDAgentMessage.append() 的语义化封装
- 暂不做压缩/摘要(guide.md §6.3 的 compaction),先只做有界最近窗口,
  避免在还没有真实多轮场景验证前就实现一套没人用的摘要逻辑
EOF
)"
```

---

### Task 10: `app/agent/loop.py` — the standard agent loop

**Files:**
- Create: `backend/app/agent/loop.py`
- Test: `backend/tests/test_agent_loop.py`

**Interfaces:**
- Consumes: `app.agent.budget.Budget`, `app.agent.budget.BudgetExceededError` (Task 8); `app.agent.session.build_model_history`, `app.agent.session.append_assistant_message`, `app.agent.session.append_tool_result` (Task 9); `app.core.llm.chat`, `app.core.llm.ChatResult`, `app.core.llm.ToolCall` (Task 7).
- Produces (the contract every later tool-implementing task — knowledge base, CMDB/monitoring, spawn — must conform to):
  - `type ToolControl = Literal["ok", "rejected", "failed", "clarification", "pending_approval"]`
  - `@dataclass ToolResult(control: ToolControl, content: str)`
  - `type ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]`
  - `type ChatFn = Callable[..., Awaitable[ChatResult]]`
  - `@dataclass LoopOutcome(reason: Literal["final_answer", "budget_exceeded", "early_exit"], final_answer: str | None, control: ToolControl | None = None)`
  - `async def run_loop(db, *, session_id: int, model_key: str, dispatch_tool: ToolDispatcher, tools: list[dict[str, Any]] | None = None, budget: Budget | None = None, chat_fn: ChatFn = chat) -> LoopOutcome`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_loop.py`:

```python
"""Tests for the standard agent loop (app.agent.loop.run_loop)."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import Budget
from app.agent.loop import LoopOutcome, ToolResult, run_loop
from app.agent.session import append_user_message, build_model_history
from app.core.llm import ChatMessage, ChatResult, ToolCall
from app.crud.agent_session import agent_session_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def _never_called_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
    raise AssertionError(f"dispatch_tool should not have been called with {name!r}")


async def test_loop_returns_final_answer_when_model_calls_no_tools(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "10.0.0.5 在线吗")
    await db_session.commit()

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(
            content="在线", tool_calls=[], finish_reason="stop", prompt_tokens=5, completion_tokens=2
        )

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=_never_called_dispatch,
        chat_fn=fake_chat,
    )
    await db_session.commit()

    assert outcome == LoopOutcome(reason="final_answer", final_answer="在线")

    history = await build_model_history(db_session, session_id)
    assert history[-1].role == "assistant"
    assert history[-1].content == "在线"


async def test_loop_dispatches_tool_and_continues_to_final_answer(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "10.0.0.5 在线吗")
    await db_session.commit()

    call_count = {"n": 0}

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ChatResult(
                content=None,
                tool_calls=[
                    ToolCall(id="call_1", name="query_monitor_status", arguments='{"ip": "10.0.0.5"}')
                ],
                finish_reason="tool_calls",
                prompt_tokens=10,
                completion_tokens=4,
            )
        return ChatResult(
            content="10.0.0.5 在线",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=15,
            completion_tokens=3,
        )

    async def fake_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        assert name == "query_monitor_status"
        assert args == {"ip": "10.0.0.5"}
        return ToolResult(control="ok", content="10.0.0.5 状态: up")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=fake_dispatch,
        chat_fn=fake_chat,
    )
    await db_session.commit()

    assert outcome.reason == "final_answer"
    assert outcome.final_answer == "10.0.0.5 在线"

    history = await build_model_history(db_session, session_id)
    roles = [m.role for m in history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert history[2].tool_call_id == "call_1"
    assert history[2].content == "10.0.0.5 状态: up"


async def test_loop_stops_early_on_pending_approval(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "重启一下 SW-12")
    await db_session.commit()

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(
            content=None,
            tool_calls=[ToolCall(id="call_1", name="propose_remediation", arguments="{}")],
            finish_reason="tool_calls",
            prompt_tokens=10,
            completion_tokens=4,
        )

    async def fake_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(control="pending_approval", content="已创建提案,等待审批")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=fake_dispatch,
        chat_fn=fake_chat,
    )

    assert outcome.reason == "early_exit"
    assert outcome.control == "pending_approval"
    assert outcome.final_answer is None


async def test_loop_stops_when_budget_exceeded(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "一直查一直查")
    await db_session.commit()

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(
            content=None,
            tool_calls=[ToolCall(id="call_x", name="query_monitor_status", arguments="{}")],
            finish_reason="tool_calls",
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def fake_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(control="ok", content="继续查")

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=fake_dispatch,
        chat_fn=fake_chat,
        budget=Budget(max_steps=2, max_cost_usd=100.0),
    )

    assert outcome == LoopOutcome(reason="budget_exceeded", final_answer=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_loop.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent.loop'`.

- [ ] **Step 3: Implement the module**

Create `backend/app/agent/loop.py`:

```python
"""The standard agent loop (docs/guide.md §2.1).

Call the model, dispatch every requested tool call, append results, repeat
until the model returns a final answer (no tool_calls), a tool signals an
early-exit control, or the budget is exhausted. The model decides *what* to
call; whether the call is allowed is decided entirely inside `dispatch_tool`
(docs/guide.md §3.1) — this loop never inspects tool arguments itself.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import Budget, BudgetExceededError
from app.agent.session import append_assistant_message, append_tool_result, build_model_history
from app.core.llm import ChatResult, ToolCall, chat

type ToolControl = Literal["ok", "rejected", "failed", "clarification", "pending_approval"]

_EARLY_EXIT_CONTROLS: frozenset[ToolControl] = frozenset({"clarification", "pending_approval", "rejected"})


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The structured result every tool dispatch must return (docs/guide.md §2.3)."""

    control: ToolControl
    content: str


type ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]
type ChatFn = Callable[..., Awaitable[ChatResult]]


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """Why the loop stopped, and its final text if it produced one."""

    reason: Literal["final_answer", "budget_exceeded", "early_exit"]
    final_answer: str | None
    control: ToolControl | None = None


def _parse_arguments(tool_call: ToolCall) -> dict[str, Any]:
    """Parse a tool call's JSON argument string, never raising into the loop."""
    try:
        parsed = json.loads(tool_call.arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def run_loop(
    db: AsyncSession,
    *,
    session_id: int,
    model_key: str,
    dispatch_tool: ToolDispatcher,
    tools: list[dict[str, Any]] | None = None,
    budget: Budget | None = None,
    chat_fn: ChatFn = chat,
) -> LoopOutcome:
    """Run one standard agent loop turn against `session_id`'s transcript."""
    active_budget = budget or Budget()

    while True:
        try:
            active_budget.record_step()
        except BudgetExceededError:
            return LoopOutcome(reason="budget_exceeded", final_answer=None)

        history = await build_model_history(db, session_id)
        result: ChatResult = await chat_fn(model_key, history, tools=tools)

        if not result.tool_calls:
            await append_assistant_message(db, session_id, result.content or "")
            return LoopOutcome(reason="final_answer", final_answer=result.content)

        await append_assistant_message(
            db, session_id, result.content or "", tool_calls=result.tool_calls
        )

        for tool_call in result.tool_calls:
            tool_result = await dispatch_tool(tool_call.name, _parse_arguments(tool_call))
            await append_tool_result(db, session_id, tool_call.id, tool_result.content)
            if tool_result.control in _EARLY_EXIT_CONTROLS:
                return LoopOutcome(
                    reason="early_exit", final_answer=None, control=tool_result.control
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_loop.py -v`
Expected: `4 passed`

- [ ] **Step 5: Run the entire new agent test suite together**

Run: `uv run pytest tests/test_agent_models.py tests/test_agent_crud_session.py tests/test_agent_crud_message.py tests/test_agent_crud_registry.py tests/test_agent_crud_hitl.py tests/test_agent_crud_trace.py tests/test_agent_llm.py tests/test_agent_budget.py tests/test_agent_session.py tests/test_agent_loop.py -v`
Expected: all pass, no interaction/ordering failures between files.

- [ ] **Step 6: Run the full existing suite one more time**

Run: `uv run pytest -v`
Expected: every test in the repository (RBAC + new agent tests) passes.

- [ ] **Step 7: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 8: Commit**

```bash
git add backend/app/agent/loop.py backend/tests/test_agent_loop.py
git commit -m "$(cat <<'EOF'
新增 app/agent/loop.py 标准 Agent 循环

- run_loop() 落地 docs/guide.md §2.1 的标准循环:调模型->没有
  tool_calls 就结束->有就逐个 dispatch_tool 并把结果追加回transcript
  ->遇到 clarification/pending_approval/rejected 立即提前退出->
  预算耗尽立即停止,不重试
- ToolResult/ToolControl/ToolDispatcher 是后续 T07(知识库工具)、
  T08(CMDB/监控工具)、T09(spawn 编排)都要遵守的统一工具契约
- chat_fn 参数可注入(默认是 app.core.llm.chat),dispatch_tool 由
  调用方传入,这样测试完全不需要真的连 LLM 或实现任何真实工具,
  4 个测试覆盖了:直接给最终答案、调一次工具后给最终答案、
  pending_approval 提前退出、预算耗尽提前退出
- T06(Agent 内核基建)到这里完成:数据模型、CRUD、llm.py、budget、
  session、loop 全部就位且可独立测试,后续 T07/T08/T09/T10/T11 都建
  在这一层之上
EOF
)"
```

---

## After This Plan

T06 is complete when all 10 tasks are committed and `uv run pytest -v`, `uv run mypy app`, and `uv run ruff check .` are clean on the whole `backend/` tree. Per `docs/AGENT_ARCHITECTURE.md` §14's dependency graph, T07 (knowledge base), T08 (CMDB + monitoring), T10 (HITL API + permission codes), and T11 (frontend) can all start once this lands — they were deliberately not designed here to keep this plan focused on one buildable, testable subsystem. Each should get its own plan via `superpowers:writing-plans` when you're ready to start it.
