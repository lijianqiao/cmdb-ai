# T08 · CMDB + 监控子系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the确定性 data + monitoring layer for ops: a lightweight CMDB (assets + dependency graph), a TCP-probe sweep that keeps device online/offline state fresh, a CMDB↔monitoring drift detector, and three read-only Agent tools (`query_cmdb`, `query_cmdb_dependencies`, `query_monitor_status`) conforming to T06's `ToolResult` contract.

**Architecture:** Per `docs/AGENT_ARCHITECTURE.md` §1's core distinction — "在线吗" is a query, not a task — this entire subsystem is deterministic code, not an Agent. `monitor_sweep` and `cmdb_diff` are periodic `asyncio` background tasks started from `app/main.py`'s `lifespan`, each with a single testable "run one pass" function underneath a thin infinite-loop wrapper. Device online/offline state is never stored as a separately-mutable column — it is always derived from the latest `MonitorStatusEvent` row via a `ROW_NUMBER() OVER (PARTITION BY target_id ...)` window function (portable across SQLite and Postgres, so — unlike T07's pgvector work — nothing in this plan needs a `TEST_POSTGRES_DATABASE_URL`-gated test file). The three Agent tools are thin read-only wrappers over the CRUD layer, mirroring T07's `kb_semantic_search` (needs `db: AsyncSession`, returns `ToolResult`).

**Tech Stack:** Python 3.14.3, FastAPI, SQLAlchemy 2 async, PostgreSQL + Alembic, `asyncio` (TCP probing — no new dependency), pytest + pytest-asyncio, uv.

**Explicitly out of scope for this plan** (per `docs/AGENT_ARCHITECTURE.md`'s task graph and this project's established restraint — see T06/T07's own scope headers for the same pattern):
- Any HTTP API for creating/editing CMDB assets or monitor targets. `docs/AGENT_ARCHITECTURE.md`'s T08 row lists only "确定性管道 + 只读工具" — no API. Rows get created directly via CRUD calls in tests for this plan; a management API is a natural follow-up once there's a real UI consumer for it (unlike T07, where the upload API was explicitly named in scope).
- WebSocket push of state transitions. `docs/AGENT_ARCHITECTURE.md` §7 describes broadcasting a state flip over WebSocket, but no WS endpoint exists yet (that lands with the frontend integration work) — this plan only writes the event to the database.
- Wiring `query_cmdb`/`query_cmdb_dependencies`/`query_monitor_status` into a live `run_loop` call via a real `ToolDispatcher` closure — same deferral T07 made for its four knowledge tools.
- ICMP probing. TCP-connect only, per the decision already made and recorded in `docs/AGENT_ARCHITECTURE.md` assumption A4.

## Global Constraints

- Python `>=3.14,<3.15` only; every command runs as `uv run <cmd>` from `backend/` — never bare `python`/`pytest`.
- mypy strict mode is on — every function needs complete type hints and an explicit return type.
- PEP 695 syntax is expected: `type Alias = ...`, `def func[T](...)`.
- ruff: line-length 100, rule sets `E, F, W, I, N, B, UP, ASYNC` (E501 is ignored project-wide).
- CRUD/service methods only `db.flush()` — **except** the top-level orchestration functions in this plan (`run_monitor_sweep_once`, `run_cmdb_diff_once`), which own their own `db.commit()` because nothing above them in the call stack ever will — they are periodic background jobs, not request handlers under an API route. This mirrors the existing precedent in `backend/init_db.py`'s `seed_permissions()`/`init_superuser()`, which also commit themselves for the same reason.
- All timestamp columns: `default=lambda: datetime.now(UTC)` **and** `server_default=func.now()` together.
- Relation tables (`cmdb_asset_dependencies`) follow this project's existing convention (see `user_roles`/`role_permissions`): only foreign keys + `created_at`, no surrogate `id`, composite primary key.
- Device/target online status is **never** stored as a mutable column — always derived from the latest `MonitorStatusEvent` per target via a window-function query. Do not add a `current_status` field to `MonitorTarget` even if it looks convenient; two sources of truth for the same fact is exactly the anti-pattern `docs/AGENT_ARCHITECTURE.md` §3 rules out.
- Commit messages: Chinese, a concise title line, blank line, then bullet points explaining what changed and why. **Never** a `Co-Authored-By` line.
- Test runner: `uv run pytest tests/<file>.py -v` (from `backend/`). Type/lint check: `uv run mypy app` and `uv run ruff check .`.
- Background loop testing convention: every `while True: ...` periodic loop in this plan has its single-iteration body extracted into a plain, directly-testable `async def run_x_once(db: AsyncSession) -> int` function (returns a count, for logging/testing). The infinite-loop wrapper (`run_x_loop`) is a thin `while True: await run_x_once(...); await asyncio.sleep(interval)` and is wired into `app/main.py`'s `lifespan` — it is not itself unit-tested (an infinite loop under pytest would hang); its correctness rests entirely on the tested `run_x_once` body plus manual verification that `lifespan` starts/cancels it correctly (see Tasks 6-7).

---

### Task 1: Data models + Alembic migration

**Files:**
- Create: `backend/app/models/cmdb_asset.py`
- Create: `backend/app/models/cmdb_asset_dependency.py`
- Create: `backend/app/models/monitor_target.py`
- Create: `backend/app/models/monitor_status_event.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/2026_08_11_1600-b3d8f1a4c672_cmdb_monitoring.py`
- Test: `backend/tests/test_ops_models.py`

**Interfaces:**
- Consumes: `app.models.base.Base`, `app.models.base.TimestampMixin`, `app.models.user.User` (FK target only).
- Produces (used by every later task):
  - `CmdbAsset(id, asset_type, hostname, ip_address, location, owner_user_id, business_system, subnet_cidr, notes, is_deleted, created_at, updated_at)`
  - `CmdbAssetDependency(parent_asset_id, child_asset_id, relation_type, created_at)` — composite PK
  - `MonitorTarget(id, cmdb_asset_id, ip_address, port, label, check_interval_seconds, is_active, created_at)`
  - `MonitorStatusEvent(id, target_id, status, latency_ms, detail, checked_at)`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_ops_models.py`:

```python
"""Structural tests for the CMDB + monitoring ORM models."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cmdb_asset import CmdbAsset
from app.models.cmdb_asset_dependency import CmdbAssetDependency
from app.models.monitor_status_event import MonitorStatusEvent
from app.models.monitor_target import MonitorTarget
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_cmdb_asset_round_trip(db_session: AsyncSession, test_user: User) -> None:
    asset = CmdbAsset(
        asset_type="switch",
        hostname="sw-core-01",
        ip_address="10.0.0.1",
        location="机房A-机柜3",
        owner_user_id=test_user.id,
        business_system="网络基础设施",
        subnet_cidr="10.0.0.0/24",
        notes="",
    )
    db_session.add(asset)
    await db_session.commit()

    stored = (await db_session.execute(select(CmdbAsset).where(CmdbAsset.id == asset.id))).scalar_one()
    assert stored.hostname == "sw-core-01"
    assert stored.is_deleted is False


async def test_cmdb_asset_dependency_composite_key(db_session: AsyncSession) -> None:
    parent = CmdbAsset(asset_type="switch", hostname="sw-01", ip_address="10.0.0.1", subnet_cidr="")
    child = CmdbAsset(asset_type="server", hostname="srv-01", ip_address="10.0.0.2", subnet_cidr="")
    db_session.add_all([parent, child])
    await db_session.flush()

    dependency = CmdbAssetDependency(
        parent_asset_id=parent.id, child_asset_id=child.id, relation_type="uplink"
    )
    db_session.add(dependency)
    await db_session.commit()

    stored = (
        await db_session.execute(
            select(CmdbAssetDependency).where(
                CmdbAssetDependency.parent_asset_id == parent.id,
                CmdbAssetDependency.child_asset_id == child.id,
            )
        )
    ).scalar_one()
    assert stored.relation_type == "uplink"


async def test_monitor_target_defaults(db_session: AsyncSession) -> None:
    asset = CmdbAsset(asset_type="server", hostname="srv-02", ip_address="10.0.0.5", subnet_cidr="")
    db_session.add(asset)
    await db_session.flush()

    target = MonitorTarget(cmdb_asset_id=asset.id, ip_address="10.0.0.5", port=22, label="SSH")
    db_session.add(target)
    await db_session.commit()

    stored = (await db_session.execute(select(MonitorTarget).where(MonitorTarget.id == target.id))).scalar_one()
    assert stored.is_active is True
    assert stored.check_interval_seconds == 30


async def test_monitor_target_allows_ad_hoc_ip_without_cmdb_asset(db_session: AsyncSession) -> None:
    target = MonitorTarget(cmdb_asset_id=None, ip_address="10.0.0.99", port=80, label="临时探测")
    db_session.add(target)
    await db_session.commit()

    stored = (await db_session.execute(select(MonitorTarget).where(MonitorTarget.id == target.id))).scalar_one()
    assert stored.cmdb_asset_id is None


async def test_monitor_status_event_round_trip(db_session: AsyncSession) -> None:
    target = MonitorTarget(cmdb_asset_id=None, ip_address="10.0.0.5", port=22, label="")
    db_session.add(target)
    await db_session.flush()

    event = MonitorStatusEvent(target_id=target.id, status="up", latency_ms=12, detail="")
    db_session.add(event)
    await db_session.commit()

    stored = (
        await db_session.execute(select(MonitorStatusEvent).where(MonitorStatusEvent.id == event.id))
    ).scalar_one()
    assert stored.status == "up"
    assert stored.latency_ms == 12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ops_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.cmdb_asset'`.

- [ ] **Step 3: Write the model files**

Create `backend/app/models/cmdb_asset.py`:

```python
"""CMDB asset — a lightweight configuration-item record.

Not a full ITIL CMDB: just enough fields to answer "who owns this / where is
it / what business system does it belong to" (docs/AGENT_ARCHITECTURE.md §3).
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class CmdbAsset(Base, TimestampMixin):
    """One managed asset (server, switch, router, ...)."""

    __tablename__ = "cmdb_assets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    asset_type: Mapped[str] = mapped_column(String(50), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False, index=True)
    location: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    owner_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    business_system: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    subnet_cidr: Mapped[str] = mapped_column(String(45), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    def __repr__(self) -> str:
        return f"<CmdbAsset(id={self.id}, hostname={self.hostname!r}, ip={self.ip_address!r})>"
```

Create `backend/app/models/cmdb_asset_dependency.py`:

```python
"""CMDB asset dependency edge — e.g. a switch (parent) hosting servers (children).

Composite primary key, no surrogate id, matching this project's existing
relation-table convention (see UserRole/RolePermission).
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CmdbAssetDependency(Base):
    """One directed dependency edge between two CMDB assets."""

    __tablename__ = "cmdb_asset_dependencies"

    parent_asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cmdb_assets.id", ondelete="CASCADE"), primary_key=True
    )
    child_asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cmdb_assets.id", ondelete="CASCADE"), primary_key=True
    )
    relation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return (
            f"<CmdbAssetDependency(parent={self.parent_asset_id}, "
            f"child={self.child_asset_id}, relation={self.relation_type!r})>"
        )
```

Create `backend/app/models/monitor_target.py`:

```python
"""Monitor target — one (ip, port) pair the sweep probes on a schedule.

`cmdb_asset_id` is nullable: a target can watch an ad-hoc IP not yet
registered in the CMDB (docs/AGENT_ARCHITECTURE.md §3).
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MonitorTarget(Base):
    """One TCP-probe target."""

    __tablename__ = "monitor_targets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cmdb_asset_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cmdb_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    check_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"<MonitorTarget(id={self.id}, ip={self.ip_address!r}, port={self.port})>"
```

Create `backend/app/models/monitor_status_event.py`:

```python
"""Monitor status event — append-only probe result log.

A target's "current" online/offline status is never stored separately; it is
always derived from the latest row here (see app/crud/monitor_status_event.py
`get_latest_status_for_targets`), per docs/AGENT_ARCHITECTURE.md §3's rule
against maintaining two sources of truth for the same fact.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MonitorStatusEvent(Base):
    """One probe result for one target."""

    __tablename__ = "monitor_status_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    target_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("monitor_targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        index=True,
    )

    def __repr__(self) -> str:
        return f"<MonitorStatusEvent(target_id={self.target_id}, status={self.status!r})>"
```

- [ ] **Step 4: Register the new models**

Modify `backend/app/models/__init__.py` — replace full contents with:

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
from app.models.cmdb_asset import CmdbAsset
from app.models.cmdb_asset_dependency import CmdbAssetDependency
from app.models.hitl_proposal import HitlProposal
from app.models.knowledge_category import KnowledgeCategory
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.monitor_status_event import MonitorStatusEvent
from app.models.monitor_target import MonitorTarget
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
    "KnowledgeCategory",
    "KnowledgeDocument",
    "KnowledgeChunk",
    "CmdbAsset",
    "CmdbAssetDependency",
    "MonitorTarget",
    "MonitorStatusEvent",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_ops_models.py -v`
Expected: `5 passed`

- [ ] **Step 6: Write the Alembic migration**

Create `backend/alembic/versions/2026_08_11_1600-b3d8f1a4c672_cmdb_monitoring.py`:

```python
"""Add CMDB + monitoring tables: assets, dependencies, targets, status events.

Revision ID: b3d8f1a4c672
Revises: a8c3f7e29d41
Create Date: 2026-08-11 16:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "b3d8f1a4c672"
down_revision: str | None = "a8c3f7e29d41"
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
        "cmdb_assets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_type", sa.String(length=50), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=False),
        sa.Column("location", sa.String(length=200), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("business_system", sa.String(length=100), nullable=False),
        sa.Column("subnet_cidr", sa.String(length=45), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cmdb_assets_ip_address"), "cmdb_assets", ["ip_address"], unique=False)
    op.create_index(op.f("ix_cmdb_assets_is_deleted"), "cmdb_assets", ["is_deleted"], unique=False)

    op.create_table(
        "cmdb_asset_dependencies",
        sa.Column("parent_asset_id", sa.Integer(), nullable=False),
        sa.Column("child_asset_id", sa.Integer(), nullable=False),
        sa.Column("relation_type", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["parent_asset_id"], ["cmdb_assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["child_asset_id"], ["cmdb_assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("parent_asset_id", "child_asset_id"),
    )

    op.create_table(
        "monitor_targets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("cmdb_asset_id", sa.Integer(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("check_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["cmdb_asset_id"], ["cmdb_assets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_monitor_targets_cmdb_asset_id"), "monitor_targets", ["cmdb_asset_id"], unique=False
    )
    op.create_index(
        op.f("ix_monitor_targets_is_active"), "monitor_targets", ["is_active"], unique=False
    )

    op.create_table(
        "monitor_status_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["target_id"], ["monitor_targets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_monitor_status_events_target_id"),
        "monitor_status_events",
        ["target_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_monitor_status_events_checked_at"),
        "monitor_status_events",
        ["checked_at"],
        unique=False,
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_table("monitor_status_events")
    op.drop_table("monitor_targets")
    op.drop_table("cmdb_asset_dependencies")
    op.drop_table("cmdb_assets")
```

- [ ] **Step 7: Apply the migration**

Run: `uv run alembic upgrade head`
Expected: last line `Running upgrade a8c3f7e29d41 -> b3d8f1a4c672, Add CMDB + monitoring tables: assets, dependencies, targets, status events`, exit 0. Requires the docker-compose Postgres to be running (`docker compose up -d` from the repo root if needed).

- [ ] **Step 8: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 9: Commit**

```bash
git add backend/app/models/cmdb_asset.py backend/app/models/cmdb_asset_dependency.py backend/app/models/monitor_target.py backend/app/models/monitor_status_event.py backend/app/models/__init__.py backend/alembic/versions/2026_08_11_1600-b3d8f1a4c672_cmdb_monitoring.py backend/tests/test_ops_models.py
git commit -m "$(cat <<'EOF'
新增 CMDB + 监控核心数据模型

- 新增 4 张表：cmdb_assets（资产元数据）、cmdb_asset_dependencies
  （依赖图，跟 user_roles/role_permissions 一样只有外键+created_at
  的关联表约定）、monitor_targets（探活目标，cmdb_asset_id 可空,
  支持监控还没登记进 CMDB 的临时 IP）、monitor_status_events
  （探活结果日志，只追加）
- 设备"当前在线状态"不单独存字段，永远从 monitor_status_events
  最新一条派生——这个决定写进了全局约束，避免出现两份可能不一致
  的状态源
EOF
)"
```

---

### Task 2: CRUD — CmdbAsset

**Files:**
- Create: `backend/app/crud/cmdb_asset.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_cmdb_crud_asset.py`

**Interfaces:**
- Consumes: `app.crud.base.CRUDBase`, `app.models.cmdb_asset.CmdbAsset` (Task 1).
- Produces: `cmdb_asset_crud: CRUDCmdbAsset` singleton with `get`/`create`/`update`/`soft_delete` (from `CRUDBase` — has `is_deleted`), plus `get_by_ip(db, ip_address) -> CmdbAsset | None`, `list_all(db) -> list[CmdbAsset]`, `list_by_business_system(db, business_system) -> list[CmdbAsset]`, `list_by_ids(db, ids: list[int]) -> list[CmdbAsset]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_cmdb_crud_asset.py`:

```python
"""CRUD tests for CmdbAsset."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud

pytestmark = pytest.mark.asyncio


async def _make_asset(
    db_session: AsyncSession, *, hostname: str, ip: str, business_system: str = ""
) -> int:
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "server",
            "hostname": hostname,
            "ip_address": ip,
            "business_system": business_system,
            "subnet_cidr": "",
        },
    )
    await db_session.flush()
    return asset.id


async def test_get_by_ip(db_session: AsyncSession) -> None:
    asset_id = await _make_asset(db_session, hostname="srv-01", ip="10.0.0.5")
    await db_session.commit()

    fetched = await cmdb_asset_crud.get_by_ip(db_session, "10.0.0.5")
    assert fetched is not None
    assert fetched.id == asset_id


async def test_get_by_ip_ignores_soft_deleted(db_session: AsyncSession) -> None:
    asset_id = await _make_asset(db_session, hostname="srv-02", ip="10.0.0.6")
    await db_session.flush()
    await cmdb_asset_crud.soft_delete(db_session, asset_id)
    await db_session.commit()

    fetched = await cmdb_asset_crud.get_by_ip(db_session, "10.0.0.6")
    assert fetched is None


async def test_list_all_excludes_soft_deleted(db_session: AsyncSession) -> None:
    kept_id = await _make_asset(db_session, hostname="srv-03", ip="10.0.0.7")
    removed_id = await _make_asset(db_session, hostname="srv-04", ip="10.0.0.8")
    await db_session.flush()
    await cmdb_asset_crud.soft_delete(db_session, removed_id)
    await db_session.commit()

    assets = await cmdb_asset_crud.list_all(db_session)

    assert {a.id for a in assets} == {kept_id}


async def test_list_by_business_system_filters(db_session: AsyncSession) -> None:
    await _make_asset(db_session, hostname="srv-05", ip="10.0.0.9", business_system="财务系统")
    await _make_asset(db_session, hostname="srv-06", ip="10.0.0.10", business_system="OA系统")
    await db_session.commit()

    finance_assets = await cmdb_asset_crud.list_by_business_system(db_session, "财务系统")

    assert len(finance_assets) == 1
    assert finance_assets[0].hostname == "srv-05"


async def test_list_by_ids_preserves_only_requested(db_session: AsyncSession) -> None:
    first_id = await _make_asset(db_session, hostname="srv-07", ip="10.0.0.11")
    second_id = await _make_asset(db_session, hostname="srv-08", ip="10.0.0.12")
    await _make_asset(db_session, hostname="srv-09", ip="10.0.0.13")
    await db_session.commit()

    assets = await cmdb_asset_crud.list_by_ids(db_session, [first_id, second_id])

    assert {a.id for a in assets} == {first_id, second_id}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cmdb_crud_asset.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.cmdb_asset'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/cmdb_asset.py`:

```python
"""CRUD operations for CMDB assets."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.cmdb_asset import CmdbAsset


class CRUDCmdbAsset(CRUDBase[CmdbAsset]):
    """CMDB asset persistence; generic get/create/update/soft_delete come from CRUDBase."""

    model = CmdbAsset

    async def get_by_ip(self, db: AsyncSession, ip_address: str) -> CmdbAsset | None:
        """Return one active asset by IP address, or None."""
        stmt = self._active_statement().where(CmdbAsset.ip_address == ip_address)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self, db: AsyncSession) -> list[CmdbAsset]:
        """Return every active asset, ordered by id."""
        stmt = self._active_statement().order_by(CmdbAsset.id.asc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_business_system(self, db: AsyncSession, business_system: str) -> list[CmdbAsset]:
        """Return active assets tagged with a given business system."""
        stmt = self._active_statement().where(CmdbAsset.business_system == business_system)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_ids(self, db: AsyncSession, ids: list[int]) -> list[CmdbAsset]:
        """Return active assets among the given ids."""
        if not ids:
            return []
        stmt = self._active_statement().where(CmdbAsset.id.in_(ids))
        result = await db.execute(stmt)
        return list(result.scalars().all())


cmdb_asset_crud = CRUDCmdbAsset()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — add `cmdb_asset_crud` (alphabetical order — note `cmdb_asset` sorts before `agent_*`? No: alphabetically `agent_message` < `agent_registry` < `agent_session` < `agent_trace_event` < `audit_log` < `cmdb_asset` < `dashboard` < ...):

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.crud.audit_log import audit_log_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.dashboard import dashboard_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.crud.permission import permission_crud
from app.crud.role import role_crud
from app.crud.user import user_crud

__all__ = [
    "agent_message_crud",
    "agent_registry_crud",
    "agent_session_crud",
    "agent_trace_event_crud",
    "audit_log_crud",
    "cmdb_asset_crud",
    "dashboard_crud",
    "hitl_proposal_crud",
    "knowledge_category_crud",
    "knowledge_chunk_crud",
    "knowledge_document_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_cmdb_crud_asset.py -v`
Expected: `5 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/crud/cmdb_asset.py backend/app/crud/__init__.py backend/tests/test_cmdb_crud_asset.py
git commit -m "$(cat <<'EOF'
新增 CmdbAsset 的 CRUD 层

- CRUDCmdbAsset 继承 CRUDBase，复用 get/create/update/soft_delete
  （这张表有 is_deleted）
- 新增 get_by_ip/list_all/list_by_business_system/list_by_ids，
  都通过 _active_statement() 自动过滤软删除记录，供后面的
  query_cmdb 工具和 cmdb_diff 巡检任务使用
EOF
)"
```

---

### Task 3: CRUD — CmdbAssetDependency (依赖图遍历)

**Files:**
- Create: `backend/app/crud/cmdb_asset_dependency.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_cmdb_crud_dependency.py`

**Interfaces:**
- Consumes: `app.models.cmdb_asset_dependency.CmdbAssetDependency` (Task 1).
- Produces: `cmdb_asset_dependency_crud: CRUDCmdbAssetDependency` singleton with:
  - `create(db, *, parent_asset_id, child_asset_id, relation_type) -> CmdbAssetDependency`
  - `get_children(db, parent_asset_id) -> list[CmdbAssetDependency]`
  - `get_parents(db, child_asset_id) -> list[CmdbAssetDependency]`
  - `traverse(db, asset_id, *, direction: Literal["up", "down"], max_depth: int = 3) -> list[tuple[int, int]]` — returns `(asset_id, depth)` pairs reachable from `asset_id` (excluding itself), breadth-first, cycle-safe, capped at `max_depth`. `direction="down"` follows parent→child edges (what's below/depends on this asset); `direction="up"` follows child→parent edges (what this asset depends on/is above it).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_cmdb_crud_dependency.py`:

```python
"""CRUD tests for CmdbAssetDependency, including graph traversal."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.cmdb_asset_dependency import cmdb_asset_dependency_crud

pytestmark = pytest.mark.asyncio


async def _make_asset(db_session: AsyncSession, hostname: str) -> int:
    asset = await cmdb_asset_crud.create(
        db_session,
        {"asset_type": "server", "hostname": hostname, "ip_address": "", "subnet_cidr": ""},
    )
    await db_session.flush()
    return asset.id


async def test_get_children_and_parents(db_session: AsyncSession) -> None:
    switch_id = await _make_asset(db_session, "sw-01")
    server_id = await _make_asset(db_session, "srv-01")
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=switch_id, child_asset_id=server_id, relation_type="uplink"
    )
    await db_session.commit()

    children = await cmdb_asset_dependency_crud.get_children(db_session, switch_id)
    parents = await cmdb_asset_dependency_crud.get_parents(db_session, server_id)

    assert [c.child_asset_id for c in children] == [server_id]
    assert [p.parent_asset_id for p in parents] == [switch_id]


async def test_traverse_down_follows_chain_within_max_depth(db_session: AsyncSession) -> None:
    switch_id = await _make_asset(db_session, "sw-01")
    rack_id = await _make_asset(db_session, "rack-01")
    server_id = await _make_asset(db_session, "srv-01")
    unreachable_id = await _make_asset(db_session, "srv-02")
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=switch_id, child_asset_id=rack_id, relation_type="uplink"
    )
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=rack_id, child_asset_id=server_id, relation_type="uplink"
    )
    await db_session.commit()

    reached = await cmdb_asset_dependency_crud.traverse(
        db_session, switch_id, direction="down", max_depth=2
    )

    reached_ids = {asset_id for asset_id, _depth in reached}
    assert reached_ids == {rack_id, server_id}
    assert unreachable_id not in reached_ids
    assert dict(reached)[rack_id] == 1
    assert dict(reached)[server_id] == 2


async def test_traverse_up_follows_reverse_direction(db_session: AsyncSession) -> None:
    switch_id = await _make_asset(db_session, "sw-01")
    server_id = await _make_asset(db_session, "srv-01")
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=switch_id, child_asset_id=server_id, relation_type="uplink"
    )
    await db_session.commit()

    reached = await cmdb_asset_dependency_crud.traverse(
        db_session, server_id, direction="up", max_depth=3
    )

    assert reached == [(switch_id, 1)]


async def test_traverse_is_cycle_safe(db_session: AsyncSession) -> None:
    a_id = await _make_asset(db_session, "a")
    b_id = await _make_asset(db_session, "b")
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=a_id, child_asset_id=b_id, relation_type="uplink"
    )
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=b_id, child_asset_id=a_id, relation_type="uplink"
    )
    await db_session.commit()

    reached = await cmdb_asset_dependency_crud.traverse(db_session, a_id, direction="down", max_depth=10)

    assert {asset_id for asset_id, _depth in reached} == {b_id}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cmdb_crud_dependency.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.cmdb_asset_dependency'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/cmdb_asset_dependency.py`:

```python
"""CRUD operations for the CMDB asset dependency graph.

Not a CRUDBase subclass: this model has a composite primary key, not an
`id` column, so CRUDBase's `_id_column()` machinery does not apply.
"""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cmdb_asset_dependency import CmdbAssetDependency


class CRUDCmdbAssetDependency:
    """Dependency-edge persistence and breadth-first graph traversal."""

    model = CmdbAssetDependency

    async def create(
        self,
        db: AsyncSession,
        *,
        parent_asset_id: int,
        child_asset_id: int,
        relation_type: str,
    ) -> CmdbAssetDependency:
        """Add one dependency edge and flush."""
        edge = CmdbAssetDependency(
            parent_asset_id=parent_asset_id, child_asset_id=child_asset_id, relation_type=relation_type
        )
        db.add(edge)
        await db.flush()
        return edge

    async def get_children(self, db: AsyncSession, parent_asset_id: int) -> list[CmdbAssetDependency]:
        """Return every edge where `parent_asset_id` is the parent."""
        stmt = select(CmdbAssetDependency).where(
            CmdbAssetDependency.parent_asset_id == parent_asset_id
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_parents(self, db: AsyncSession, child_asset_id: int) -> list[CmdbAssetDependency]:
        """Return every edge where `child_asset_id` is the child."""
        stmt = select(CmdbAssetDependency).where(
            CmdbAssetDependency.child_asset_id == child_asset_id
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def traverse(
        self,
        db: AsyncSession,
        asset_id: int,
        *,
        direction: Literal["up", "down"],
        max_depth: int = 3,
    ) -> list[tuple[int, int]]:
        """Breadth-first traverse the dependency graph from `asset_id`.

        `direction="down"` follows parent->child edges; `direction="up"`
        follows child->parent edges. Returns (asset_id, depth) pairs,
        excluding the starting asset, cycle-safe, capped at `max_depth`.
        """
        visited: set[int] = {asset_id}
        frontier: list[int] = [asset_id]
        results: list[tuple[int, int]] = []
        depth = 0

        while frontier and depth < max_depth:
            depth += 1
            next_frontier: list[int] = []
            for current_id in frontier:
                if direction == "down":
                    edges = await self.get_children(db, current_id)
                    neighbor_ids = [edge.child_asset_id for edge in edges]
                else:
                    edges = await self.get_parents(db, current_id)
                    neighbor_ids = [edge.parent_asset_id for edge in edges]

                for neighbor_id in neighbor_ids:
                    if neighbor_id in visited:
                        continue
                    visited.add(neighbor_id)
                    results.append((neighbor_id, depth))
                    next_frontier.append(neighbor_id)
            frontier = next_frontier

        return results


cmdb_asset_dependency_crud = CRUDCmdbAssetDependency()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — add `cmdb_asset_dependency_crud` (alphabetical order):

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.crud.audit_log import audit_log_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.cmdb_asset_dependency import cmdb_asset_dependency_crud
from app.crud.dashboard import dashboard_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.crud.permission import permission_crud
from app.crud.role import role_crud
from app.crud.user import user_crud

__all__ = [
    "agent_message_crud",
    "agent_registry_crud",
    "agent_session_crud",
    "agent_trace_event_crud",
    "audit_log_crud",
    "cmdb_asset_crud",
    "cmdb_asset_dependency_crud",
    "dashboard_crud",
    "hitl_proposal_crud",
    "knowledge_category_crud",
    "knowledge_chunk_crud",
    "knowledge_document_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_cmdb_crud_dependency.py -v`
Expected: `4 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/crud/cmdb_asset_dependency.py backend/app/crud/__init__.py backend/tests/test_cmdb_crud_dependency.py
git commit -m "$(cat <<'EOF'
新增 CmdbAssetDependency 的 CRUD 层与依赖图遍历

- 不继承 CRUDBase：这张表是复合主键，没有 id 列，CRUDBase 的
  _id_column() 用不上
- traverse() 是广度优先遍历，direction=down 走 parent->child（"这个
  交换机下面挂了哪些设备"），direction=up 走 child->parent（"这台
  设备依赖什么"），带 visited 集合防环，max_depth 强制上限对应
  docs/AGENT_ARCHITECTURE.md §4.2 的"防止图过大拖垮上下文"要求
- 测试专门覆盖了一个真实的环（a 依赖 b、b 又依赖 a），验证遍历不会
  死循环
EOF
)"
```

---

### Task 4: CRUD — MonitorTarget

**Files:**
- Create: `backend/app/crud/monitor_target.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_monitor_crud_target.py`

**Interfaces:**
- Consumes: `app.models.monitor_target.MonitorTarget` (Task 1).
- Produces: `monitor_target_crud: CRUDMonitorTarget` singleton with `get`/`create`/`update` (from `CRUDBase` — this model has no `is_deleted`, so `soft_delete()` is unused), plus `list_active(db) -> list[MonitorTarget]` and `list_by_ip_prefix(db, ip_prefix: str) -> list[MonitorTarget]` (simple `LIKE`-based prefix match, used by `query_monitor_status`'s `ip_cidr` parameter — see Task 9 for why this is intentionally a prefix match, not real CIDR math).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_monitor_crud_target.py`:

```python
"""CRUD tests for MonitorTarget."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.monitor_target import monitor_target_crud

pytestmark = pytest.mark.asyncio


async def test_create_and_get(db_session: AsyncSession) -> None:
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "label": "SSH"}
    )
    await db_session.commit()

    fetched = await monitor_target_crud.get(db_session, target.id)
    assert fetched is not None
    assert fetched.port == 22


async def test_list_active_excludes_inactive_targets(db_session: AsyncSession) -> None:
    active = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "is_active": True}
    )
    await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.6", "port": 22, "is_active": False}
    )
    await db_session.commit()

    targets = await monitor_target_crud.list_active(db_session)

    assert [t.id for t in targets] == [active.id]


async def test_list_by_ip_prefix_matches_subnet_style_prefix(db_session: AsyncSession) -> None:
    await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22}
    )
    await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.1.5", "port": 22}
    )
    await db_session.commit()

    matches = await monitor_target_crud.list_by_ip_prefix(db_session, "10.0.0.")

    assert len(matches) == 1
    assert matches[0].ip_address == "10.0.0.5"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_monitor_crud_target.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.monitor_target'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/monitor_target.py`:

```python
"""CRUD operations for monitor targets."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.monitor_target import MonitorTarget


def _escape_like_literal(value: str) -> str:
    """Escape a literal string for safe use inside a SQL LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class CRUDMonitorTarget(CRUDBase[MonitorTarget]):
    """Monitor target persistence; generic get/create/update come from CRUDBase.

    This model has no is_deleted column, so soft_delete() is simply unused.
    """

    model = MonitorTarget

    async def list_active(self, db: AsyncSession) -> list[MonitorTarget]:
        """Return every target the sweep should probe this round."""
        stmt = select(MonitorTarget).where(MonitorTarget.is_active.is_(True))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_ip_prefix(self, db: AsyncSession, ip_prefix: str) -> list[MonitorTarget]:
        """Return targets whose IP starts with `ip_prefix` (literal prefix match, not CIDR math)."""
        pattern = f"{_escape_like_literal(ip_prefix)}%"
        stmt = select(MonitorTarget).where(MonitorTarget.ip_address.like(pattern, escape="\\"))
        result = await db.execute(stmt)
        return list(result.scalars().all())


monitor_target_crud = CRUDMonitorTarget()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — add `monitor_target_crud` (alphabetical order — note it sorts after `knowledge_document`, before `permission`):

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.crud.audit_log import audit_log_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.cmdb_asset_dependency import cmdb_asset_dependency_crud
from app.crud.dashboard import dashboard_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.crud.monitor_target import monitor_target_crud
from app.crud.permission import permission_crud
from app.crud.role import role_crud
from app.crud.user import user_crud

__all__ = [
    "agent_message_crud",
    "agent_registry_crud",
    "agent_session_crud",
    "agent_trace_event_crud",
    "audit_log_crud",
    "cmdb_asset_crud",
    "cmdb_asset_dependency_crud",
    "dashboard_crud",
    "hitl_proposal_crud",
    "knowledge_category_crud",
    "knowledge_chunk_crud",
    "knowledge_document_crud",
    "monitor_target_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_monitor_crud_target.py -v`
Expected: `3 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/crud/monitor_target.py backend/app/crud/__init__.py backend/tests/test_monitor_crud_target.py
git commit -m "$(cat <<'EOF'
新增 MonitorTarget 的 CRUD 层

- CRUDMonitorTarget 继承 CRUDBase，这张表没有 is_deleted，
  soft_delete() 不会被用到
- list_active() 给 sweep 每轮取要探测的目标；list_by_ip_prefix() 用
  已有的 contains_pattern() 转义工具做字面前缀匹配，query_monitor_
  status 工具用它模拟"网段"查询——不是真的 CIDR 数学运算，只是字符
  串前缀匹配，够用且简单
EOF
)"
```

---

### Task 5: CRUD — MonitorStatusEvent（含"最新状态"派生查询）

**Files:**
- Create: `backend/app/crud/monitor_status_event.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_monitor_crud_status_event.py`

**Interfaces:**
- Consumes: `app.models.monitor_status_event.MonitorStatusEvent` (Task 1).
- Produces: `monitor_status_event_crud: CRUDMonitorStatusEvent` singleton with:
  - `record(db, *, target_id, status, latency_ms=None, detail="") -> MonitorStatusEvent`
  - `list_recent_for_target(db, target_id, *, limit=20) -> list[MonitorStatusEvent]` (newest-first)
  - `get_latest_status_for_targets(db, target_ids: list[int]) -> dict[int, MonitorStatusEvent]` — one most-recent event per target, keyed by `target_id`; targets with no events yet are simply absent from the returned dict. Implemented with a portable `ROW_NUMBER() OVER (PARTITION BY target_id ORDER BY checked_at DESC)` window function (works on both SQLite and Postgres — no `TEST_POSTGRES_DATABASE_URL` gating needed anywhere in this plan).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_monitor_crud_status_event.py`:

```python
"""CRUD tests for MonitorStatusEvent, including the latest-status window query."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud

pytestmark = pytest.mark.asyncio


async def _make_target(db_session: AsyncSession, ip: str = "10.0.0.5") -> int:
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": ip, "port": 22}
    )
    await db_session.flush()
    return target.id


async def test_record_and_list_recent_newest_first(db_session: AsyncSession) -> None:
    target_id = await _make_target(db_session)

    await monitor_status_event_crud.record(db_session, target_id=target_id, status="up", latency_ms=5)
    await monitor_status_event_crud.record(
        db_session, target_id=target_id, status="down", detail="连接被拒绝"
    )
    await db_session.commit()

    events = await monitor_status_event_crud.list_recent_for_target(db_session, target_id)

    assert [e.status for e in events] == ["down", "up"]


async def test_get_latest_status_for_targets_returns_most_recent_per_target(
    db_session: AsyncSession,
) -> None:
    target_a = await _make_target(db_session, "10.0.0.5")
    target_b = await _make_target(db_session, "10.0.0.6")

    await monitor_status_event_crud.record(db_session, target_id=target_a, status="up")
    await monitor_status_event_crud.record(db_session, target_id=target_a, status="down")
    await monitor_status_event_crud.record(db_session, target_id=target_b, status="up")
    await db_session.commit()

    latest = await monitor_status_event_crud.get_latest_status_for_targets(
        db_session, [target_a, target_b]
    )

    assert latest[target_a].status == "down"
    assert latest[target_b].status == "up"


async def test_get_latest_status_for_targets_omits_targets_with_no_events(
    db_session: AsyncSession,
) -> None:
    target_id = await _make_target(db_session)
    await db_session.commit()

    latest = await monitor_status_event_crud.get_latest_status_for_targets(db_session, [target_id])

    assert latest == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_monitor_crud_status_event.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.monitor_status_event'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/monitor_status_event.py`:

```python
"""CRUD operations for monitor status events (append-only).

`get_latest_status_for_targets` is this module's key query: it derives each
target's "current" status from the latest event row, using a portable
ROW_NUMBER() window function rather than Postgres-only DISTINCT ON, so this
whole subsystem needs no TEST_POSTGRES_DATABASE_URL-gated test file.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.monitor_status_event import MonitorStatusEvent


class CRUDMonitorStatusEvent:
    """Append-only status-event storage plus the latest-status derivation query."""

    model = MonitorStatusEvent

    async def record(
        self,
        db: AsyncSession,
        *,
        target_id: int,
        status: str,
        latency_ms: int | None = None,
        detail: str = "",
    ) -> MonitorStatusEvent:
        """Append one probe result and flush."""
        event = MonitorStatusEvent(
            target_id=target_id, status=status, latency_ms=latency_ms, detail=detail
        )
        db.add(event)
        await db.flush()
        return event

    async def list_recent_for_target(
        self, db: AsyncSession, target_id: int, *, limit: int = 20
    ) -> list[MonitorStatusEvent]:
        """Return a target's most recent events, newest-first."""
        stmt = (
            select(MonitorStatusEvent)
            .where(MonitorStatusEvent.target_id == target_id)
            .order_by(MonitorStatusEvent.checked_at.desc(), MonitorStatusEvent.id.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_latest_status_for_targets(
        self, db: AsyncSession, target_ids: list[int]
    ) -> dict[int, MonitorStatusEvent]:
        """Return each target's most recent event, keyed by target_id.

        Targets with no recorded events are simply absent from the result.
        """
        if not target_ids:
            return {}

        row_number = (
            func.row_number()
            .over(
                partition_by=MonitorStatusEvent.target_id,
                order_by=(MonitorStatusEvent.checked_at.desc(), MonitorStatusEvent.id.desc()),
            )
            .label("rn")
        )
        ranked = (
            select(MonitorStatusEvent, row_number)
            .where(MonitorStatusEvent.target_id.in_(target_ids))
            .subquery()
        )
        latest = aliased(MonitorStatusEvent, ranked)
        stmt = select(latest).where(ranked.c.rn == 1)

        result = await db.execute(stmt)
        events = result.scalars().all()
        return {event.target_id: event for event in events}


monitor_status_event_crud = CRUDMonitorStatusEvent()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — add `monitor_status_event_crud` (alphabetical order, right before `monitor_target_crud`):

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.crud.audit_log import audit_log_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.cmdb_asset_dependency import cmdb_asset_dependency_crud
from app.crud.dashboard import dashboard_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.crud.permission import permission_crud
from app.crud.role import role_crud
from app.crud.user import user_crud

__all__ = [
    "agent_message_crud",
    "agent_registry_crud",
    "agent_session_crud",
    "agent_trace_event_crud",
    "audit_log_crud",
    "cmdb_asset_crud",
    "cmdb_asset_dependency_crud",
    "dashboard_crud",
    "hitl_proposal_crud",
    "knowledge_category_crud",
    "knowledge_chunk_crud",
    "knowledge_document_crud",
    "monitor_status_event_crud",
    "monitor_target_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_monitor_crud_status_event.py -v`
Expected: `3 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/crud/monitor_status_event.py backend/app/crud/__init__.py backend/tests/test_monitor_crud_status_event.py
git commit -m "$(cat <<'EOF'
新增 MonitorStatusEvent 的 CRUD 层与"最新状态"派生查询

- record()/list_recent_for_target() 是常规的追加+按时间倒序查询
- get_latest_status_for_targets() 是这张表的关键查询：用
  ROW_NUMBER() OVER (PARTITION BY target_id ORDER BY checked_at DESC)
  取每个 target 最新一条事件，这个写法 SQLite 和 Postgres 都支持
  （不像 pgvector 的 cosine_distance 那样只能在 Postgres 跑），所以
  T08 整个计划都不需要 TEST_POSTGRES_DATABASE_URL 门控测试
- 没有事件记录的 target 会直接不出现在返回的 dict 里，调用方（sweep
  和 cmdb_diff）要自己处理"从来没探测过"这种情况
EOF
)"
```

---

### Task 6: TCP 探活 + 常驻 sweep 后台任务

**Files:**
- Create: `backend/app/services/monitor_sweep.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/.env.example`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_monitor_sweep.py`

**Interfaces:**
- Consumes: `app.crud.monitor_target.monitor_target_crud.list_active`, `app.crud.monitor_status_event.monitor_status_event_crud.record` (Tasks 4-5), `app.core.database.AsyncSessionLocal`.
- Produces:
  - `async def probe_tcp(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]` — returns `(status, latency_ms, detail)`, `status` is `"up"` or `"down"`
  - `async def run_monitor_sweep_once(db: AsyncSession) -> int` — probes every active target once, records one event per target, commits, returns the count probed
  - `async def run_monitor_sweep_loop(*, interval_seconds: float | None = None) -> None` — infinite loop wrapper, wired into `app/main.py`'s `lifespan`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_monitor_sweep.py`:

```python
"""Tests for the TCP probe and the single-pass monitor sweep."""

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.services.monitor_sweep import probe_tcp, run_monitor_sweep_once

pytestmark = pytest.mark.asyncio


async def _start_echo_server() -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _get_closed_port() -> int:
    """Bind then immediately release a port so it's (very likely) free but not listening."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_probe_tcp_reports_up_for_listening_port() -> None:
    server, port = await _start_echo_server()
    try:
        status, latency_ms, detail = await probe_tcp("127.0.0.1", port, timeout_seconds=2.0)
    finally:
        server.close()
        await server.wait_closed()

    assert status == "up"
    assert latency_ms is not None
    assert latency_ms >= 0
    assert detail == ""


async def test_probe_tcp_reports_down_for_closed_port() -> None:
    port = _get_closed_port()

    status, latency_ms, detail = await probe_tcp("127.0.0.1", port, timeout_seconds=2.0)

    assert status == "down"
    assert latency_ms is None
    assert detail != ""


async def test_probe_tcp_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr("app.services.monitor_sweep.asyncio.open_connection", hang)

    status, latency_ms, detail = await probe_tcp("127.0.0.1", 9, timeout_seconds=0.05)

    assert status == "down"
    assert latency_ms is None
    assert detail == "连接超时"


async def test_run_monitor_sweep_once_records_one_event_per_active_target(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "is_active": True}
    )
    await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.6", "port": 22, "is_active": False}
    )
    await db_session.commit()

    async def fake_probe(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]:
        return "up", 3, ""

    monkeypatch.setattr("app.services.monitor_sweep.probe_tcp", fake_probe)

    probed_count = await run_monitor_sweep_once(db_session)

    assert probed_count == 1
    events = await monitor_status_event_crud.list_recent_for_target(db_session, active.id)
    assert len(events) == 1
    assert events[0].status == "up"


async def test_run_monitor_sweep_once_continues_after_one_target_probe_raises(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "is_active": True}
    )
    healthy = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.6", "port": 22, "is_active": True}
    )
    await db_session.commit()

    async def flaky_probe(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]:
        if ip == "10.0.0.5":
            raise OSError("network unreachable")
        return "up", 1, ""

    monkeypatch.setattr("app.services.monitor_sweep.probe_tcp", flaky_probe)

    probed_count = await run_monitor_sweep_once(db_session)

    assert probed_count == 2
    failing_events = await monitor_status_event_crud.list_recent_for_target(db_session, failing.id)
    healthy_events = await monitor_status_event_crud.list_recent_for_target(db_session, healthy.id)
    assert failing_events[0].status == "down"
    assert healthy_events[0].status == "up"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_monitor_sweep.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.monitor_sweep'`.

- [ ] **Step 3: Add monitor settings to config.py**

Modify `backend/app/core/config.py` — insert this block immediately after the LLM block (after the `LLM_EMBEDDING_MODEL` line, currently line 50) and before the blank line preceding `# JWT / 会话` (currently line 51-52):

```python
    LLM_EMBEDDING_BASE_URL: str = "http://127.0.0.1:8080/v1"
    LLM_EMBEDDING_API_KEY: SecretStr | None = None
    LLM_EMBEDDING_MODEL: str = "Qwen3-Embedding-0.6B"

    # 运维监控：TCP 探活 + CMDB 差异巡检
    MONITOR_PROBE_TIMEOUT_SECONDS: float = Field(default=3.0, gt=0, le=30)
    MONITOR_SWEEP_INTERVAL_SECONDS: float = Field(default=30.0, ge=5, le=3600)
    CMDB_DIFF_INTERVAL_SECONDS: float = Field(default=3600.0, ge=60, le=86_400)

    # JWT / 会话
```

- [ ] **Step 4: Document the new env vars**

Modify `backend/.env.example` — insert this block right after the embedding-model lines and before the `# 在开发中` comment block:

```ini
LLM_EMBEDDING_MODEL=Qwen3-Embedding-0.6B

# 运维监控：TCP 探活 + CMDB 差异巡检
MONITOR_PROBE_TIMEOUT_SECONDS=3.0
MONITOR_SWEEP_INTERVAL_SECONDS=30.0
CMDB_DIFF_INTERVAL_SECONDS=3600.0
```

- [ ] **Step 5: Implement the probe + sweep module**

Create `backend/app/services/monitor_sweep.py`:

```python
"""TCP-connect probing and the single-pass monitor sweep.

`run_monitor_sweep_once` is the deterministic core (docs/guide.md §1.3: this
is a rules-clear, reproducible task, not something an Agent should decide how
to do). `run_monitor_sweep_loop` is a thin infinite wrapper wired into
app/main.py's lifespan — it is not itself unit-tested (see this plan's Global
Constraints).
"""

import asyncio
import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud

logger = logging.getLogger(__name__)


async def probe_tcp(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]:
    """Attempt a TCP connect; return (status, latency_ms, detail).

    status is "up" on a successful connect, "down" on timeout or any
    connection error. latency_ms is None when the probe did not succeed.
    """
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout_seconds
        )
    except TimeoutError:
        return "down", None, "连接超时"
    except OSError as exc:
        return "down", None, str(exc)

    latency_ms = int((time.monotonic() - start) * 1000)
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return "up", latency_ms, ""


async def run_monitor_sweep_once(db: AsyncSession) -> int:
    """Probe every active target once, record one status event each, commit.

    A probe failure for one target is logged and recorded as "down" (with the
    exception text as detail) rather than aborting the whole sweep — one bad
    target must not stop the others from being checked.
    """
    targets = await monitor_target_crud.list_active(db)
    for target in targets:
        try:
            status, latency_ms, detail = await probe_tcp(
                target.ip_address, target.port, timeout_seconds=settings.MONITOR_PROBE_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - a single target's probe must never abort the sweep
            status, latency_ms, detail = "down", None, str(exc)

        await monitor_status_event_crud.record(
            db, target_id=target.id, status=status, latency_ms=latency_ms, detail=detail
        )

    await db.commit()
    return len(targets)


async def run_monitor_sweep_loop(*, interval_seconds: float | None = None) -> None:
    """Run `run_monitor_sweep_once` forever, sleeping `interval_seconds` between rounds."""
    interval = (
        interval_seconds if interval_seconds is not None else settings.MONITOR_SWEEP_INTERVAL_SECONDS
    )
    while True:
        try:
            async with AsyncSessionLocal() as db:
                count = await run_monitor_sweep_once(db)
                logger.info("monitor sweep 完成，探测 %d 个目标", count)
        except Exception:
            logger.exception("monitor sweep 单轮失败")
        await asyncio.sleep(interval)
```

- [ ] **Step 6: Wire the sweep into `app/main.py`'s lifespan**

Modify `backend/app/main.py` — update the imports and the `lifespan` function:

```python
"""FastAPI application factory and cross-cutting HTTP policies."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.database import engine
from app.core.security import PasswordHashOverloadedError
from app.crud.base import RelatedObjectsNotFoundError
from app.crud.role import RoleInUseError
from app.crud.user import LastActiveSuperuserError
from app.services.monitor_sweep import run_monitor_sweep_loop
```

Then replace the `lifespan` function:

```python
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Run the monitor sweep for the app's lifetime, then release pooled connections."""
    monitor_task = asyncio.create_task(run_monitor_sweep_loop())
    yield
    monitor_task.cancel()
    with suppress(asyncio.CancelledError):
        await monitor_task
    await engine.dispose()
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/test_monitor_sweep.py -v`
Expected: `5 passed`

- [ ] **Step 8: Run the full existing suite to confirm the config/main.py changes didn't break anything**

Run: `uv run pytest -v`
Expected: all previously-passing tests still pass — this is the step that catches a broken `extra="forbid"` settings declaration or a `lifespan` regression before it's committed.

- [ ] **Step 9: Manually verify the app still starts with the sweep running**

Run: `uv run python main.py` (from `backend/`, this uses the Windows-safe `SelectorEventLoop` entry point) in the background for a few seconds, then stop it (Ctrl+C). Expected: no startup errors; log lines like `monitor sweep 完成，探测 0 个目标` appear roughly every `MONITOR_SWEEP_INTERVAL_SECONDS` (0 targets is correct — none exist yet in this database); shutdown is clean (no "Task was destroyed but it is pending" warnings, confirming the `lifespan` cancellation path works).

- [ ] **Step 10: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 11: Commit**

```bash
git add backend/app/services/monitor_sweep.py backend/app/core/config.py backend/.env.example backend/app/main.py backend/tests/test_monitor_sweep.py
git commit -m "$(cat <<'EOF'
新增 TCP 探活与常驻 sweep 后台任务

- probe_tcp() 就是 asyncio.open_connection + wait_for 超时，成功记
  up+延迟，超时/连接错误记 down+原因，不用 ICMP（对应
  docs/AGENT_ARCHITECTURE.md 假设 A4 已定的第一期方案）
- run_monitor_sweep_once() 是可单测的单轮核心：遍历所有 is_active
  的 target，每个都探测一次并记一条事件，单个 target 探测异常不会
  中断整轮巡检（用 except Exception 兜底记成 down，不让一个坏目标
  拖垮其它目标的探测）
- run_monitor_sweep_loop() 是薄薄的 while True 包装，接进
  app/main.py 的 lifespan：应用启动时创建后台任务，关闭时 cancel 并
  等待退出，跟现有的 engine.dispose() 收尾顺序一致
- config.py 新增 MONITOR_PROBE_TIMEOUT_SECONDS/
  MONITOR_SWEEP_INTERVAL_SECONDS/CMDB_DIFF_INTERVAL_SECONDS 三个配置
EOF
)"
```

---

### Task 7: CMDB 差异巡检任务

**Files:**
- Create: `backend/app/services/cmdb_diff.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_cmdb_diff.py`

**Interfaces:**
- Consumes: `app.crud.cmdb_asset.cmdb_asset_crud.list_all`, `app.crud.monitor_target.monitor_target_crud.list_active`, `app.crud.monitor_status_event.monitor_status_event_crud.get_latest_status_for_targets` (Tasks 2, 4, 5), `app.utils.audit.log_audit`.
- Produces: `async def run_cmdb_diff_once(db: AsyncSession) -> int` (returns finding count, commits), `async def run_cmdb_diff_loop(*, interval_seconds: float | None = None) -> None` (wired into `app/main.py`'s `lifespan` alongside the monitor sweep task).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_cmdb_diff.py`:

```python
"""Tests for the CMDB <-> monitoring drift detector."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.models.audit_log import AuditLog
from app.services.cmdb_diff import run_cmdb_diff_once

pytestmark = pytest.mark.asyncio


async def test_flags_reachable_ip_not_in_cmdb(db_session: AsyncSession) -> None:
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.99", "port": 80}
    )
    await db_session.flush()
    await monitor_status_event_crud.record(db_session, target_id=target.id, status="up")
    await db_session.commit()

    findings = await run_cmdb_diff_once(db_session)

    assert findings == 1
    logs = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "cmdb_drift_detected"))
    ).scalars().all()
    assert len(logs) == 1
    assert "10.0.0.99" in logs[0].detail


async def test_flags_cmdb_asset_never_reachable(db_session: AsyncSession) -> None:
    await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "server",
            "hostname": "srv-ghost",
            "ip_address": "10.0.0.50",
            "subnet_cidr": "",
        },
    )
    await db_session.commit()

    findings = await run_cmdb_diff_once(db_session)

    assert findings == 1
    logs = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "cmdb_drift_detected"))
    ).scalars().all()
    assert len(logs) == 1
    assert "10.0.0.50" in logs[0].detail


async def test_no_findings_when_cmdb_and_monitoring_agree(db_session: AsyncSession) -> None:
    asset = await cmdb_asset_crud.create(
        db_session,
        {"asset_type": "server", "hostname": "srv-ok", "ip_address": "10.0.0.10", "subnet_cidr": ""},
    )
    await db_session.flush()
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": asset.id, "ip_address": "10.0.0.10", "port": 22}
    )
    await db_session.flush()
    await monitor_status_event_crud.record(db_session, target_id=target.id, status="up")
    await db_session.commit()

    findings = await run_cmdb_diff_once(db_session)

    assert findings == 0


async def test_does_not_modify_cmdb_or_monitor_tables(db_session: AsyncSession) -> None:
    """Drift detection only logs — it never creates/deletes CmdbAsset or MonitorTarget rows."""
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.99", "port": 80}
    )
    await db_session.flush()
    await monitor_status_event_crud.record(db_session, target_id=target.id, status="up")
    await db_session.commit()

    await run_cmdb_diff_once(db_session)

    assets = await cmdb_asset_crud.list_all(db_session)
    assert assets == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cmdb_diff.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.cmdb_diff'`.

- [ ] **Step 3: Implement the diff module**

Create `backend/app/services/cmdb_diff.py`:

```python
"""CMDB <-> monitoring drift detection.

Compares "reachable IPs with no matching active CmdbAsset" (shadow assets)
against "active CmdbAsset entries never observed reachable" (stale entries).
Only logs findings via the existing audit_logs table — never creates,
updates, or deletes CmdbAsset/MonitorTarget rows itself
(docs/AGENT_ARCHITECTURE.md §7: automated confirmation stays out of this
job's hands; a human or a future HITL-gated proposal reconciles drift).
"""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.models.monitor_target import MonitorTarget
from app.utils.audit import log_audit

logger = logging.getLogger(__name__)


async def run_cmdb_diff_once(db: AsyncSession) -> int:
    """Compare CMDB assets against monitor targets; log drift; commit; return finding count."""
    assets = await cmdb_asset_crud.list_all(db)
    targets = await monitor_target_crud.list_active(db)
    latest_status = await monitor_status_event_crud.get_latest_status_for_targets(
        db, [t.id for t in targets]
    )

    asset_ips = {asset.ip_address for asset in assets}
    reachable_ips = {
        target.ip_address
        for target in targets
        if target.id in latest_status and latest_status[target.id].status == "up"
    }

    findings = 0

    for ip in sorted(reachable_ips - asset_ips):
        await log_audit(
            db,
            None,
            "cmdb_drift_detected",
            target=f"ip:{ip}",
            detail=f"探测到在线但 CMDB 未登记的资产: {ip}",
            ip="local",
        )
        findings += 1

    ip_to_targets: dict[str, list[MonitorTarget]] = {}
    for target in targets:
        ip_to_targets.setdefault(target.ip_address, []).append(target)

    for asset in assets:
        asset_targets = ip_to_targets.get(asset.ip_address, [])
        ever_reachable = any(
            target.id in latest_status and latest_status[target.id].status == "up"
            for target in asset_targets
        )
        if not ever_reachable:
            await log_audit(
                db,
                None,
                "cmdb_drift_detected",
                target=f"cmdb_asset:{asset.id}",
                detail=f"CMDB 登记的资产从未探测到在线: {asset.ip_address}",
                ip="local",
            )
            findings += 1

    await db.commit()
    return findings


async def run_cmdb_diff_loop(*, interval_seconds: float | None = None) -> None:
    """Run `run_cmdb_diff_once` forever, sleeping `interval_seconds` between rounds.

    Sleeps first (unlike the monitor sweep, this job is not urgent on startup).
    """
    interval = interval_seconds if interval_seconds is not None else settings.CMDB_DIFF_INTERVAL_SECONDS
    while True:
        await asyncio.sleep(interval)
        try:
            async with AsyncSessionLocal() as db:
                count = await run_cmdb_diff_once(db)
                if count:
                    logger.info("cmdb diff 巡检发现 %d 条差异", count)
        except Exception:
            logger.exception("cmdb diff 单轮失败")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cmdb_diff.py -v`
Expected: `4 passed`

- [ ] **Step 5: Wire the diff job into `app/main.py`'s lifespan alongside the monitor sweep**

Modify `backend/app/main.py` — add the import:

```python
from app.services.cmdb_diff import run_cmdb_diff_loop
from app.services.monitor_sweep import run_monitor_sweep_loop
```

(replacing the single `from app.services.monitor_sweep import run_monitor_sweep_loop` line from Task 6 with both imports, alphabetically ordered)

Then update `lifespan` to start both tasks:

```python
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Run the monitor sweep and CMDB diff jobs for the app's lifetime, then release connections."""
    monitor_task = asyncio.create_task(run_monitor_sweep_loop())
    diff_task = asyncio.create_task(run_cmdb_diff_loop())
    yield
    for task in (monitor_task, diff_task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await engine.dispose()
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -v`
Expected: all pass, zero regressions.

- [ ] **Step 7: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/cmdb_diff.py backend/app/main.py backend/tests/test_cmdb_diff.py
git commit -m "$(cat <<'EOF'
新增 CMDB 差异巡检任务，接进 lifespan

- run_cmdb_diff_once() 对比两类差异：探测到在线但 CMDB 没登记的
  IP（疑似影子资产），和 CMDB 登记了但从来没探测到在线的资产（疑似
  过期登记）；只写 audit_logs（action=cmdb_drift_detected），不自动
  增删 CMDB 记录——这个决定写在模块 docstring 里，避免以后有人图
  方便让巡检任务自己"修复"数据
- run_cmdb_diff_loop() 跟 monitor sweep 一样接进 app/main.py 的
  lifespan，但先 sleep 再跑第一轮（这个任务不紧急，不用启动就抢跑）
EOF
)"
```

---

### Task 8: `query_cmdb` + `query_cmdb_dependencies` 工具

**Files:**
- Create: `backend/app/agent/ops_tools.py`
- Test: `backend/tests/test_ops_tools_cmdb.py`

**Interfaces:**
- Consumes: `app.agent.loop.ToolResult` (T06), `app.crud.cmdb_asset.cmdb_asset_crud`, `app.crud.cmdb_asset_dependency.cmdb_asset_dependency_crud` (Tasks 2-3).
- Produces:
  - `async def query_cmdb(db: AsyncSession, *, asset_ids: list[int] | None = None, ip: str | None = None, business_system: str | None = None) -> ToolResult`
  - `async def query_cmdb_dependencies(db: AsyncSession, asset_id: int, *, direction: Literal["up", "down"] = "down", max_depth: int = 3) -> ToolResult`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_ops_tools_cmdb.py`:

```python
"""Tests for query_cmdb and query_cmdb_dependencies."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ops_tools import query_cmdb, query_cmdb_dependencies
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.cmdb_asset_dependency import cmdb_asset_dependency_crud

pytestmark = pytest.mark.asyncio


async def _make_asset(db_session: AsyncSession, hostname: str, ip: str, business_system: str = "") -> int:
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "server",
            "hostname": hostname,
            "ip_address": ip,
            "business_system": business_system,
            "subnet_cidr": "",
        },
    )
    await db_session.flush()
    return asset.id


async def test_query_cmdb_by_ip_returns_asset_details(db_session: AsyncSession) -> None:
    await _make_asset(db_session, "srv-01", "10.0.0.5", business_system="财务系统")
    await db_session.commit()

    result = await query_cmdb(db_session, ip="10.0.0.5")

    assert result.control == "ok"
    assert "srv-01" in result.content
    assert "财务系统" in result.content


async def test_query_cmdb_no_filters_returns_all(db_session: AsyncSession) -> None:
    await _make_asset(db_session, "srv-01", "10.0.0.5")
    await _make_asset(db_session, "srv-02", "10.0.0.6")
    await db_session.commit()

    result = await query_cmdb(db_session)

    assert result.control == "ok"
    assert "srv-01" in result.content
    assert "srv-02" in result.content


async def test_query_cmdb_reports_no_matches(db_session: AsyncSession) -> None:
    result = await query_cmdb(db_session, ip="10.0.0.99")

    assert result.control == "ok"
    assert result.content == "没有找到匹配的资产"


async def test_query_cmdb_dependencies_reports_chain(db_session: AsyncSession) -> None:
    switch_id = await _make_asset(db_session, "sw-01", "10.0.0.1")
    server_id = await _make_asset(db_session, "srv-01", "10.0.0.5")
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=switch_id, child_asset_id=server_id, relation_type="uplink"
    )
    await db_session.commit()

    result = await query_cmdb_dependencies(db_session, switch_id, direction="down")

    assert result.control == "ok"
    assert "srv-01" in result.content


async def test_query_cmdb_dependencies_reports_empty_graph(db_session: AsyncSession) -> None:
    asset_id = await _make_asset(db_session, "srv-01", "10.0.0.5")
    await db_session.commit()

    result = await query_cmdb_dependencies(db_session, asset_id, direction="down")

    assert result.control == "ok"
    assert result.content == "没有找到依赖关系"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ops_tools_cmdb.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent.ops_tools'`.

- [ ] **Step 3: Implement the tools**

Create `backend/app/agent/ops_tools.py`:

```python
"""Agent-facing read-only tools for CMDB and monitoring (docs/AGENT_ARCHITECTURE.md §4.2).

All three tools need `db: AsyncSession` (unlike T07's filesystem-backed
kb_glob/kb_read/kb_grep) since they query structured data, matching
kb_semantic_search's precedent from T07. None of them are wired into a real
ToolDispatcher closure yet — that lands with whichever task first invokes
app.agent.loop.run_loop for real (see this plan's header).
"""

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import ToolResult
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.cmdb_asset_dependency import cmdb_asset_dependency_crud
from app.models.cmdb_asset import CmdbAsset


def _format_asset(asset: CmdbAsset) -> str:
    return (
        f"[id={asset.id}] {asset.hostname} ({asset.ip_address}) "
        f"类型={asset.asset_type} 位置={asset.location or '未填写'} "
        f"业务系统={asset.business_system or '未填写'} 备注={asset.notes or '无'}"
    )


async def query_cmdb(
    db: AsyncSession,
    *,
    asset_ids: list[int] | None = None,
    ip: str | None = None,
    business_system: str | None = None,
) -> ToolResult:
    """Look up CMDB assets by id list, IP, or business system; no filter returns everything."""
    if asset_ids is not None:
        assets = await cmdb_asset_crud.list_by_ids(db, asset_ids)
    elif ip is not None:
        found = await cmdb_asset_crud.get_by_ip(db, ip)
        assets = [found] if found is not None else []
    elif business_system is not None:
        assets = await cmdb_asset_crud.list_by_business_system(db, business_system)
    else:
        assets = await cmdb_asset_crud.list_all(db)

    if not assets:
        return ToolResult(control="ok", content="没有找到匹配的资产")
    return ToolResult(control="ok", content="\n".join(_format_asset(a) for a in assets))


async def query_cmdb_dependencies(
    db: AsyncSession,
    asset_id: int,
    *,
    direction: Literal["up", "down"] = "down",
    max_depth: int = 3,
) -> ToolResult:
    """Traverse the CMDB dependency graph from `asset_id`."""
    reached = await cmdb_asset_dependency_crud.traverse(
        db, asset_id, direction=direction, max_depth=max_depth
    )
    if not reached:
        return ToolResult(control="ok", content="没有找到依赖关系")

    reached_ids = [asset_id for asset_id, _depth in reached]
    assets_by_id = {a.id: a for a in await cmdb_asset_crud.list_by_ids(db, reached_ids)}
    depth_by_id = dict(reached)

    lines = [
        f"[深度={depth_by_id[a_id]}] {_format_asset(assets_by_id[a_id])}"
        for a_id in reached_ids
        if a_id in assets_by_id
    ]
    return ToolResult(control="ok", content="\n".join(lines))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ops_tools_cmdb.py -v`
Expected: `5 passed`

- [ ] **Step 5: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agent/ops_tools.py backend/tests/test_ops_tools_cmdb.py
git commit -m "$(cat <<'EOF'
新增 query_cmdb / query_cmdb_dependencies 两个运维 Agent 工具

- 新建 app/agent/ops_tools.py，跟 T07 的 knowledge_tools.py 是同一种
  角色定位：薄封装 CRUD 层，返回 loop.py 的 ToolResult
- query_cmdb 支持按 asset_ids/ip/business_system 三选一过滤，都不给
  就返回全部资产
- query_cmdb_dependencies 复用 Task 3 的 traverse()，把 (asset_id,
  depth) 结果格式化成带深度标注的资产描述文本
EOF
)"
```

---

### Task 9: `query_monitor_status` 工具

**Files:**
- Modify: `backend/app/agent/ops_tools.py`
- Test: `backend/tests/test_ops_tools_monitor.py`

**Interfaces:**
- Consumes: `app.agent.loop.ToolResult`, `app.crud.monitor_target.monitor_target_crud`, `app.crud.monitor_status_event.monitor_status_event_crud` (Tasks 4-5).
- Produces: `async def query_monitor_status(db: AsyncSession, *, target_ids: list[int] | None = None, ip_prefix: str | None = None, since_limit: int = 5) -> ToolResult`. Note: the plan's earlier interface sketch (and `docs/AGENT_ARCHITECTURE.md`) named this parameter `ip_cidr`; this task renames it to `ip_prefix` because the implementation (Task 4's `list_by_ip_prefix`) is honestly a literal string-prefix match, not real CIDR arithmetic — call it what it does, per this project's naming-matches-behavior convention, rather than promising CIDR semantics it doesn't have.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_ops_tools_monitor.py`:

```python
"""Tests for query_monitor_status."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ops_tools import query_monitor_status
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud

pytestmark = pytest.mark.asyncio


async def _make_target_with_status(db_session: AsyncSession, ip: str, status: str) -> int:
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": ip, "port": 22, "label": ip}
    )
    await db_session.flush()
    await monitor_status_event_crud.record(db_session, target_id=target.id, status=status)
    return target.id


async def test_query_monitor_status_by_target_ids(db_session: AsyncSession) -> None:
    target_id = await _make_target_with_status(db_session, "10.0.0.5", "up")
    await db_session.commit()

    result = await query_monitor_status(db_session, target_ids=[target_id])

    assert result.control == "ok"
    assert "10.0.0.5" in result.content
    assert "up" in result.content


async def test_query_monitor_status_by_ip_prefix(db_session: AsyncSession) -> None:
    await _make_target_with_status(db_session, "10.0.0.5", "down")
    await _make_target_with_status(db_session, "10.0.1.5", "up")
    await db_session.commit()

    result = await query_monitor_status(db_session, ip_prefix="10.0.0.")

    assert "10.0.0.5" in result.content
    assert "down" in result.content
    assert "10.0.1.5" not in result.content


async def test_query_monitor_status_reports_never_checked_target(db_session: AsyncSession) -> None:
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.9", "port": 22}
    )
    await db_session.commit()

    result = await query_monitor_status(db_session, target_ids=[target.id])

    assert result.control == "ok"
    assert "尚未探测" in result.content


async def test_query_monitor_status_reports_no_targets_found(db_session: AsyncSession) -> None:
    result = await query_monitor_status(db_session, ip_prefix="192.168.")

    assert result.control == "ok"
    assert result.content == "没有找到匹配的监控目标"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ops_tools_monitor.py -v`
Expected: FAIL with `ImportError: cannot import name 'query_monitor_status' from 'app.agent.ops_tools'`.

- [ ] **Step 3: Implement `query_monitor_status`**

Modify `backend/app/agent/ops_tools.py` — add these imports to the top (merge with existing imports):

```python
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.models.monitor_target import MonitorTarget
```

Append to the end of `backend/app/agent/ops_tools.py`:

```python
async def query_monitor_status(
    db: AsyncSession,
    *,
    target_ids: list[int] | None = None,
    ip_prefix: str | None = None,
    since_limit: int = 5,
) -> ToolResult:
    """Report each target's current status (derived from its latest event) plus recent history.

    `ip_prefix` is a literal string-prefix match (see app/crud/monitor_target.py
    `list_by_ip_prefix`), not real CIDR arithmetic — named to match what it
    actually does.
    """
    targets: list[MonitorTarget]
    if target_ids is not None:
        results = [await monitor_target_crud.get(db, tid) for tid in target_ids]
        targets = [t for t in results if t is not None]
    elif ip_prefix is not None:
        targets = await monitor_target_crud.list_by_ip_prefix(db, ip_prefix)
    else:
        targets = await monitor_target_crud.list_active(db)

    if not targets:
        return ToolResult(control="ok", content="没有找到匹配的监控目标")

    latest_status = await monitor_status_event_crud.get_latest_status_for_targets(
        db, [t.id for t in targets]
    )

    lines: list[str] = []
    for target in targets:
        header = f"[id={target.id}] {target.ip_address}:{target.port} ({target.label or '未命名'})"
        latest = latest_status.get(target.id)
        if latest is None:
            lines.append(f"{header} — 尚未探测")
            continue

        recent = await monitor_status_event_crud.list_recent_for_target(
            db, target.id, limit=since_limit
        )
        history = ", ".join(f"{event.status}@{event.checked_at:%H:%M:%S}" for event in recent)
        latency_text = f"{latest.latency_ms}ms" if latest.latency_ms is not None else "—"
        lines.append(
            f"{header} — 当前: {latest.status} (延迟 {latency_text}); 最近记录: {history}"
        )

    return ToolResult(control="ok", content="\n".join(lines))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ops_tools_monitor.py -v`
Expected: `4 passed`

- [ ] **Step 5: Run the CMDB tools test file again to confirm no regression**

Run: `uv run pytest tests/test_ops_tools_cmdb.py -v`
Expected: `5 passed` (unchanged from Task 8 — confirms the file edit didn't disturb `query_cmdb`/`query_cmdb_dependencies`).

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -v`
Expected: all pass, zero regressions.

- [ ] **Step 7: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 8: Commit**

```bash
git add backend/app/agent/ops_tools.py backend/tests/test_ops_tools_monitor.py
git commit -m "$(cat <<'EOF'
新增 query_monitor_status 运维 Agent 工具

- 支持按 target_ids 或 ip_prefix（字面前缀匹配，不是真 CIDR，参数名
  故意不叫 ip_cidr 以免承诺没实现的语义）过滤，都不给就查全部
  is_active 目标
- 当前状态从 get_latest_status_for_targets 派生，从来没探测过的
  target 明确报"尚未探测"而不是当成 down，避免跟"探测到离线"混淆
- 附带最近 N 条事件历史（默认 5 条），方便 Agent 判断是不是在抖动
EOF
)"
```

---

## After This Plan

T08 is complete when all 9 tasks are committed and `uv run pytest -v`, `uv run mypy app`, and `uv run ruff check .` are clean on the whole `backend/` tree — notably, **no test in this plan needs `TEST_POSTGRES_DATABASE_URL`**, unlike T07's pgvector work. Per `docs/AGENT_ARCHITECTURE.md` §14's dependency graph, T09 (spawn orchestration — including the `ops_explorer`/`investigator` roles and the parallel multi-machine-room diagnosis flow) depends on **both** this plan and T07 being done, since it needs both subsystems' read tools together. T10 (HITL API — including the `device_control` executor stub this plan's `docs/AGENT_ARCHITECTURE.md` origin explicitly deferred) depends only on T06 and can be built independently of this plan. A CMDB/monitor-target management API (create/edit assets and targets through HTTP, since this plan deliberately left that out) is a natural, independent follow-up whenever there's a real UI consumer for it.
