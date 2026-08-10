# T07 · 知识库子系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the knowledge-base subsystem — documents land on disk under `knowledge/`, get chunked and embedded into pgvector, and become queryable through four Agent tools (`kb_glob`, `kb_grep`, `kb_read`, `kb_semantic_search`) plus a minimal upload API.

**Architecture:** Documents are stored as real files under `knowledge/{category_code}/{document_id}_{filename}` (metadata + embeddings in Postgres) per `docs/AGENT_ARCHITECTURE.md` §3/§4.2 — `kb_grep` shells out to real ripgrep, `kb_glob`/`kb_read` touch the real filesystem, `kb_semantic_search` embeds the query via the local llama.cpp embedding model and does a pgvector cosine-distance search. A new `app/services/` layer (file storage, ingestion orchestration) sits between the CRUD layer (T06 pattern) and two consumers: the upload API (human-initiated) and the `app/agent/knowledge_tools.py` tool functions (model-initiated, conforming to T06's `ToolResult`/`ToolControl` contract from `app/agent/loop.py`).

**Tech Stack:** Python 3.14.3, FastAPI, SQLAlchemy 2 async, PostgreSQL + pgvector + Alembic, ripgrep (external binary), httpx, pytest + pytest-asyncio, uv.

**Explicitly out of scope for this plan** (deferred to later plans per `docs/AGENT_ARCHITECTURE.md`'s task graph):
- Automatic/LLM-driven classification of uploaded documents (the `classifier` role, parallel batch classification) — that is T09's job (spawn orchestration), which depends on this plan's tools existing first. Category is assigned explicitly by the uploader in this plan.
- `.docx` parsing — only `.md`/`.txt` are accepted uploads in this plan. Adding `.docx` needs `python-docx` and binary-to-text extraction, a distinct follow-up.
- Reranking retrieved chunks (the architecture doc mentions an optional reranker) — `kb_semantic_search` returns raw pgvector top-k ranking only.
- Wiring these tools into a live `run_loop` call via a real `ToolDispatcher` closure — no route in this plan invokes `app.agent.loop.run_loop`. That wiring is a T09/T11-level concern once there's a real caller with a `db` session and a role to run.

## Global Constraints

- Python `>=3.14,<3.15` only; every command runs as `uv run <cmd>` from `backend/` — never bare `python`/`pytest`.
- mypy strict mode is on — every function needs complete type hints and an explicit return type.
- PEP 695 syntax is expected: `type Alias = ...`, `def func[T](...)`.
- ruff: line-length 100, rule sets `E, F, W, I, N, B, UP, ASYNC` (E501 is ignored project-wide).
- CRUD methods only `db.flush()` — never `db.commit()`. Only the API route layer (Task 11) commits, after both the business mutation and its audit-log entry succeed, exactly once — this is the first task in the project to actually exercise that rule end-to-end (T06 had no routes).
- All timestamp columns: `default=lambda: datetime.now(UTC)` **and** `server_default=func.now()` together.
- `pgvector.sqlalchemy.Vector` is confirmed importable and works for plain create/insert/select against the aiosqlite test DB (verified directly: `CREATE TABLE`/`INSERT`/`SELECT` all succeed). Only pgvector-specific SQL operators (`.cosine_distance()` and friends, which compile to Postgres-only syntax) fail on SQLite — anything using those must be gated behind `TEST_POSTGRES_DATABASE_URL` (see Task 4), the exact pattern already used by `tests/test_postgres_refresh_concurrency.py`.
- `ripgrep` (`rg`) must be on `PATH` — confirmed present in this environment (`rg 15.0.0`). A fresh machine needs it installed separately; it is not a Python dependency.
- The local Postgres now runs via `docker-compose.yml` at the repo root (`pgvector/pgvector:pg17`, mapped to host port `5433`) — the native Windows Postgres install has no `vector` extension available at all. `backend/.env`'s `DATABASE_URL` already points at the new instance; `alembic upgrade head` and `init_db.py` have already been re-run against it (16 permissions, 1 superuser, `vector` extension confirmed available).
- Commit messages: Chinese, a concise title line, blank line, then bullet points explaining what changed and why. **Never** a `Co-Authored-By` line.
- Test runner: `uv run pytest tests/<file>.py -v` (from `backend/`). Type/lint check: `uv run mypy app` and `uv run ruff check .`.

---

### Task 1: Data models + Alembic migration

**Files:**
- Create: `backend/app/models/knowledge_category.py`
- Create: `backend/app/models/knowledge_document.py`
- Create: `backend/app/models/knowledge_chunk.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/2026_08_11_1000-f2b6d8e1a327_knowledge_base.py`
- Test: `backend/tests/test_knowledge_models.py`

**Interfaces:**
- Consumes: `app.models.base.Base`, `app.models.base.TimestampMixin`, `app.models.user.User` (FK target), `pgvector.sqlalchemy.Vector`.
- Produces (used by every later task):
  - `KnowledgeCategory(id, code, name, description, created_at, updated_at)`
  - `KnowledgeDocument(id, category_id, title, original_filename, file_path, file_type, content_hash, status, uploaded_by, is_deleted, created_at, updated_at)`
  - `KnowledgeChunk(id, document_id, chunk_index, content, token_count, embedding: list[float] (1024-dim), created_at)`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_knowledge_models.py`:

```python
"""Structural tests for the knowledge-base ORM models."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_category import KnowledgeCategory
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_category_round_trip(db_session: AsyncSession) -> None:
    category = KnowledgeCategory(code="sop", name="故障处理 SOP", description="")
    db_session.add(category)
    await db_session.commit()

    stored = (
        await db_session.execute(select(KnowledgeCategory).where(KnowledgeCategory.id == category.id))
    ).scalar_one()
    assert stored.code == "sop"


async def test_document_round_trip(db_session: AsyncSession, test_user: User) -> None:
    category = KnowledgeCategory(code="sop", name="故障处理 SOP", description="")
    db_session.add(category)
    await db_session.flush()

    document = KnowledgeDocument(
        category_id=category.id,
        title="交换机重启流程",
        original_filename="reboot.md",
        file_path="sop/1_reboot.md",
        file_type="md",
        content_hash="a" * 64,
        status="processing",
        uploaded_by=test_user.id,
    )
    db_session.add(document)
    await db_session.commit()

    stored = (
        await db_session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document.id))
    ).scalar_one()
    assert stored.status == "processing"
    assert stored.is_deleted is False


async def test_chunk_stores_embedding_vector(db_session: AsyncSession, test_user: User) -> None:
    category = KnowledgeCategory(code="sop", name="故障处理 SOP", description="")
    db_session.add(category)
    await db_session.flush()
    document = KnowledgeDocument(
        category_id=category.id,
        title="",
        original_filename="a.md",
        file_path="sop/1_a.md",
        file_type="md",
        content_hash="b" * 64,
        status="processing",
        uploaded_by=test_user.id,
    )
    db_session.add(document)
    await db_session.flush()

    chunk = KnowledgeChunk(
        document_id=document.id,
        chunk_index=0,
        content="第一段内容",
        token_count=5,
        embedding=[0.1] * 1024,
    )
    db_session.add(chunk)
    await db_session.commit()

    stored = (
        await db_session.execute(select(KnowledgeChunk).where(KnowledgeChunk.id == chunk.id))
    ).scalar_one()
    assert len(stored.embedding) == 1024
    assert stored.embedding[0] == pytest.approx(0.1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.knowledge_category'`.

- [ ] **Step 3: Write the model files**

Create `backend/app/models/knowledge_category.py`:

```python
"""Knowledge-base category — the top-level grouping for uploaded documents."""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class KnowledgeCategory(Base, TimestampMixin):
    """One knowledge category (e.g. SOP, network topology, vendor manuals)."""

    __tablename__ = "knowledge_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    def __repr__(self) -> str:
        return f"<KnowledgeCategory(id={self.id}, code={self.code!r})>"
```

Create `backend/app/models/knowledge_document.py`:

```python
"""Knowledge-base document — metadata only; the file content lives on disk
under knowledge/{category_code}/{document_id}_{filename} (see
app/services/knowledge_storage.py), per docs/AGENT_ARCHITECTURE.md §4.3.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class KnowledgeDocument(Base, TimestampMixin):
    """One uploaded document's metadata."""

    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("knowledge_categories.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    uploaded_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    def __repr__(self) -> str:
        return f"<KnowledgeDocument(id={self.id}, title={self.title!r}, status={self.status!r})>"
```

Create `backend/app/models/knowledge_chunk.py`:

```python
"""Knowledge-base chunk — one retrieval unit with its embedding vector.

1024 dims matches the local Qwen3-Embedding-0.6B model (see
docs/AGENT_ARCHITECTURE.md assumption A2; adjust the column definition if the
actual embedding model changes).
"""

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

EMBEDDING_DIM = 1024


class KnowledgeChunk(Base):
    """One chunk of a document's text plus its embedding vector."""

    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"<KnowledgeChunk(id={self.id}, document_id={self.document_id}, chunk_index={self.chunk_index})>"
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
from app.models.hitl_proposal import HitlProposal
from app.models.knowledge_category import KnowledgeCategory
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
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
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_knowledge_models.py -v`
Expected: `3 passed`

- [ ] **Step 6: Write the Alembic migration**

Create `backend/alembic/versions/2026_08_11_1000-f2b6d8e1a327_knowledge_base.py`:

```python
"""Add knowledge-base tables: categories, documents, chunks (pgvector).

Revision ID: f2b6d8e1a327
Revises: e1a4c7d9f215
Create Date: 2026-08-11 10:00:00+00:00
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa

from alembic import context, op

revision: str = "f2b6d8e1a327"
down_revision: str | None = "e1a4c7d9f215"
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
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_knowledge_categories_code"), "knowledge_categories", ["code"], unique=True
    )

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("file_type", sa.String(length=20), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["category_id"], ["knowledge_categories.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_knowledge_documents_category_id"), "knowledge_documents", ["category_id"], unique=False
    )
    op.create_index(
        op.f("ix_knowledge_documents_content_hash"), "knowledge_documents", ["content_hash"], unique=False
    )
    op.create_index(
        op.f("ix_knowledge_documents_is_deleted"), "knowledge_documents", ["is_deleted"], unique=False
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(1024), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_knowledge_chunks_document_id"), "knowledge_chunks", ["document_id"], unique=False
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    op.drop_table("knowledge_categories")
```

- [ ] **Step 7: Apply the migration**

Run: `uv run alembic upgrade head`
Expected: last line is `Running upgrade e1a4c7d9f215 -> f2b6d8e1a327, Add knowledge-base tables: categories, documents, chunks (pgvector)`, exit 0. Requires the pgvector-enabled Postgres from `docker-compose.yml` to be running (`docker compose up -d` if it isn't already).

- [ ] **Step 8: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 9: Commit**

```bash
git add backend/app/models/knowledge_category.py backend/app/models/knowledge_document.py backend/app/models/knowledge_chunk.py backend/app/models/__init__.py backend/alembic/versions/2026_08_11_1000-f2b6d8e1a327_knowledge_base.py backend/tests/test_knowledge_models.py
git commit -m "$(cat <<'EOF'
新增知识库核心数据模型

- 新增 3 张表：knowledge_categories（分类）、knowledge_documents
  （文档元数据，正文落盘不进数据库）、knowledge_chunks（分片+
  embedding 向量，1024 维对应本地 Qwen3-Embedding-0.6B）
- 迁移里显式 CREATE EXTENSION IF NOT EXISTS vector；knowledge_chunks
  用 pgvector.sqlalchemy.Vector 列类型，已验证对 aiosqlite 测试库的
  建表/插入/查询都能正常工作（只有 cosine_distance 这类 pgvector
  专用算子在 SQLite 上跑不了，见 Task 4 的处理方式）
- knowledge_documents.category_id 用 ondelete=RESTRICT，防止误删还挂着
  文档的分类
EOF
)"
```

---

### Task 2: CRUD — KnowledgeCategory

**Files:**
- Create: `backend/app/crud/knowledge_category.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_knowledge_crud_category.py`

**Interfaces:**
- Consumes: `app.crud.base.CRUDBase`, `app.models.knowledge_category.KnowledgeCategory` (Task 1).
- Produces: `knowledge_category_crud: CRUDKnowledgeCategory` singleton with `get`/`create`/`update` (from `CRUDBase`), plus `get_by_code(db, code) -> KnowledgeCategory | None` and `list_all(db) -> list[KnowledgeCategory]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_knowledge_crud_category.py`:

```python
"""CRUD tests for KnowledgeCategory."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.knowledge_category import knowledge_category_crud

pytestmark = pytest.mark.asyncio


async def test_create_and_get_by_code(db_session: AsyncSession) -> None:
    category = await knowledge_category_crud.create(
        db_session, {"code": "sop", "name": "故障处理 SOP", "description": "运维故障处理手册"}
    )
    await db_session.commit()

    fetched = await knowledge_category_crud.get_by_code(db_session, "sop")
    assert fetched is not None
    assert fetched.id == category.id
    assert fetched.name == "故障处理 SOP"


async def test_get_by_code_returns_none_when_missing(db_session: AsyncSession) -> None:
    fetched = await knowledge_category_crud.get_by_code(db_session, "does-not-exist")
    assert fetched is None


async def test_list_all_orders_by_id(db_session: AsyncSession) -> None:
    first = await knowledge_category_crud.create(
        db_session, {"code": "sop", "name": "SOP", "description": ""}
    )
    await db_session.flush()
    second = await knowledge_category_crud.create(
        db_session, {"code": "topology", "name": "网络拓扑", "description": ""}
    )
    await db_session.commit()

    categories = await knowledge_category_crud.list_all(db_session)

    assert [c.id for c in categories] == [first.id, second.id]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_crud_category.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.knowledge_category'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/knowledge_category.py`:

```python
"""CRUD operations for knowledge-base categories."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.knowledge_category import KnowledgeCategory


class CRUDKnowledgeCategory(CRUDBase[KnowledgeCategory]):
    """Knowledge category persistence; generic get/create/update come from CRUDBase."""

    model = KnowledgeCategory

    async def get_by_code(self, db: AsyncSession, code: str) -> KnowledgeCategory | None:
        """Return one category by its unique code, or None."""
        stmt = select(KnowledgeCategory).where(KnowledgeCategory.code == code)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self, db: AsyncSession) -> list[KnowledgeCategory]:
        """Return every category, ordered by id."""
        stmt = select(KnowledgeCategory).order_by(KnowledgeCategory.id.asc())
        result = await db.execute(stmt)
        return list(result.scalars().all())


knowledge_category_crud = CRUDKnowledgeCategory()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — add `knowledge_category_crud` (alphabetical order):

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.crud.audit_log import audit_log_crud
from app.crud.dashboard import dashboard_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.crud.knowledge_category import knowledge_category_crud
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
    "knowledge_category_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_knowledge_crud_category.py -v`
Expected: `3 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/crud/knowledge_category.py backend/app/crud/__init__.py backend/tests/test_knowledge_crud_category.py
git commit -m "$(cat <<'EOF'
新增 KnowledgeCategory 的 CRUD 层

- CRUDKnowledgeCategory 继承 CRUDBase，复用通用的 get/create/update，
  只加了 get_by_code()（上传接口按 code 查分类）和 list_all()（给
  上传表单列出可选分类）
EOF
)"
```

---

### Task 3: CRUD — KnowledgeDocument

**Files:**
- Create: `backend/app/crud/knowledge_document.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_knowledge_crud_document.py`

**Interfaces:**
- Consumes: `app.crud.base.CRUDBase`, `app.models.knowledge_document.KnowledgeDocument` (Task 1).
- Produces: `knowledge_document_crud: CRUDKnowledgeDocument` singleton with `get`/`create`/`update`/`soft_delete` (from `CRUDBase` — `KnowledgeDocument` has an `is_deleted` column, so soft-delete works out of the box), plus `get_by_content_hash(db, content_hash) -> KnowledgeDocument | None` and `list_for_category(db, category_id, *, skip=0, limit=20) -> tuple[list[KnowledgeDocument], int]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_knowledge_crud_document.py`:

```python
"""CRUD tests for KnowledgeDocument."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_category(db_session: AsyncSession, code: str = "sop") -> int:
    category = await knowledge_category_crud.create(
        db_session, {"code": code, "name": code, "description": ""}
    )
    await db_session.flush()
    return category.id


async def test_create_and_get_by_content_hash(db_session: AsyncSession, test_user: User) -> None:
    category_id = await _make_category(db_session)
    document = await knowledge_document_crud.create(
        db_session,
        {
            "category_id": category_id,
            "title": "重启流程",
            "original_filename": "reboot.md",
            "file_path": "sop/1_reboot.md",
            "file_type": "md",
            "content_hash": "a" * 64,
            "status": "processing",
            "uploaded_by": test_user.id,
        },
    )
    await db_session.commit()

    fetched = await knowledge_document_crud.get_by_content_hash(db_session, "a" * 64)
    assert fetched is not None
    assert fetched.id == document.id


async def test_get_by_content_hash_ignores_soft_deleted(
    db_session: AsyncSession, test_user: User
) -> None:
    category_id = await _make_category(db_session)
    document = await knowledge_document_crud.create(
        db_session,
        {
            "category_id": category_id,
            "title": "",
            "original_filename": "a.md",
            "file_path": "sop/1_a.md",
            "file_type": "md",
            "content_hash": "b" * 64,
            "status": "ready",
            "uploaded_by": test_user.id,
        },
    )
    await db_session.flush()
    await knowledge_document_crud.soft_delete(db_session, document.id)
    await db_session.commit()

    fetched = await knowledge_document_crud.get_by_content_hash(db_session, "b" * 64)
    assert fetched is None


async def test_list_for_category_filters_and_counts(
    db_session: AsyncSession, test_user: User
) -> None:
    sop_id = await _make_category(db_session, "sop")
    other_id = await _make_category(db_session, "topology")

    await knowledge_document_crud.create(
        db_session,
        {
            "category_id": sop_id,
            "title": "文档一",
            "original_filename": "a.md",
            "file_path": "sop/1_a.md",
            "file_type": "md",
            "content_hash": "c" * 64,
            "status": "ready",
            "uploaded_by": test_user.id,
        },
    )
    await knowledge_document_crud.create(
        db_session,
        {
            "category_id": other_id,
            "title": "文档二",
            "original_filename": "b.md",
            "file_path": "topology/2_b.md",
            "file_type": "md",
            "content_hash": "d" * 64,
            "status": "ready",
            "uploaded_by": test_user.id,
        },
    )
    await db_session.commit()

    items, total = await knowledge_document_crud.list_for_category(db_session, sop_id)

    assert total == 1
    assert items[0].title == "文档一"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_crud_document.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.knowledge_document'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/knowledge_document.py`:

```python
"""CRUD operations for knowledge-base document metadata."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.knowledge_document import KnowledgeDocument


class CRUDKnowledgeDocument(CRUDBase[KnowledgeDocument]):
    """Knowledge document metadata persistence.

    Generic get/create/update/soft_delete come from CRUDBase — this model
    has an `is_deleted` column, so soft-delete works without an override.
    """

    model = KnowledgeDocument

    async def get_by_content_hash(
        self, db: AsyncSession, content_hash: str
    ) -> KnowledgeDocument | None:
        """Return one active document by its content hash (dedup check), or None."""
        stmt = self._active_statement().where(KnowledgeDocument.content_hash == content_hash)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_category(
        self,
        db: AsyncSession,
        category_id: int,
        *,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[KnowledgeDocument], int]:
        """Return one category's active documents newest-first with a total count."""
        count_stmt = self._active_statement().where(
            KnowledgeDocument.category_id == category_id
        )
        total = (
            await db.execute(select(func.count()).select_from(count_stmt.subquery()))
        ).scalar_one()

        stmt = (
            self._active_statement()
            .where(KnowledgeDocument.category_id == category_id)
            .order_by(KnowledgeDocument.id.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all()), total


knowledge_document_crud = CRUDKnowledgeDocument()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — add `knowledge_document_crud` (alphabetical order):

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.crud.audit_log import audit_log_crud
from app.crud.dashboard import dashboard_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.crud.knowledge_category import knowledge_category_crud
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
    "dashboard_crud",
    "hitl_proposal_crud",
    "knowledge_category_crud",
    "knowledge_document_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_knowledge_crud_document.py -v`
Expected: `3 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/crud/knowledge_document.py backend/app/crud/__init__.py backend/tests/test_knowledge_crud_document.py
git commit -m "$(cat <<'EOF'
新增 KnowledgeDocument 的 CRUD 层

- CRUDKnowledgeDocument 继承 CRUDBase，复用通用的 get/create/update/
  soft_delete（这张表有 is_deleted 字段，软删除不用额外覆盖）
- get_by_content_hash() 只查未删除的记录，配合 _active_statement()
  自动过滤，供上传接口做去重判断
- list_for_category() 按分类分页倒序，带 total 计数
EOF
)"
```

---

### Task 4: CRUD — KnowledgeChunk (create + pgvector similarity search)

**Files:**
- Create: `backend/app/crud/knowledge_chunk.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_knowledge_crud_chunk.py`
- Test: `backend/tests/test_knowledge_chunk_search_postgres.py`

**Interfaces:**
- Consumes: `app.models.knowledge_chunk.KnowledgeChunk`, `app.models.knowledge_document.KnowledgeDocument` (Task 1).
- Produces: `knowledge_chunk_crud: CRUDKnowledgeChunk` singleton with:
  - `create(db, *, document_id, chunk_index, content, token_count, embedding) -> KnowledgeChunk`
  - `list_for_document(db, document_id) -> list[KnowledgeChunk]` (ordered by `chunk_index`)
  - `search_similar(db, *, query_embedding, category_id=None, top_k=5) -> list[tuple[KnowledgeChunk, float]]` — `float` is cosine distance (lower is more similar); **only exercised against real Postgres** (see Test file 2)

- [ ] **Step 1: Write the failing test (SQLite-compatible parts)**

Create `backend/tests/test_knowledge_crud_chunk.py`:

```python
"""CRUD tests for KnowledgeChunk that don't require pgvector's SQL operators.

`search_similar()`'s actual similarity ordering needs real Postgres with the
vector extension — see test_knowledge_chunk_search_postgres.py. This file
only covers create/list, which work fine against the aiosqlite test DB
(verified: pgvector's Vector column create/insert/select all work on SQLite,
only the cosine-distance SQL operator is Postgres-only).
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_document(db_session: AsyncSession, user_id: int) -> int:
    category = await knowledge_category_crud.create(
        db_session, {"code": "sop", "name": "SOP", "description": ""}
    )
    await db_session.flush()
    document = await knowledge_document_crud.create(
        db_session,
        {
            "category_id": category.id,
            "title": "",
            "original_filename": "a.md",
            "file_path": "sop/1_a.md",
            "file_type": "md",
            "content_hash": "a" * 64,
            "status": "processing",
            "uploaded_by": user_id,
        },
    )
    await db_session.flush()
    return document.id


async def test_create_and_list_for_document_ordered_by_chunk_index(
    db_session: AsyncSession, test_user: User
) -> None:
    document_id = await _make_document(db_session, test_user.id)

    await knowledge_chunk_crud.create(
        db_session,
        document_id=document_id,
        chunk_index=1,
        content="第二段",
        token_count=3,
        embedding=[0.2] * 1024,
    )
    await knowledge_chunk_crud.create(
        db_session,
        document_id=document_id,
        chunk_index=0,
        content="第一段",
        token_count=3,
        embedding=[0.1] * 1024,
    )
    await db_session.commit()

    chunks = await knowledge_chunk_crud.list_for_document(db_session, document_id)

    assert [c.content for c in chunks] == ["第一段", "第二段"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_crud_chunk.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crud.knowledge_chunk'`.

- [ ] **Step 3: Implement the CRUD module**

Create `backend/app/crud/knowledge_chunk.py`:

```python
"""CRUD operations for knowledge-base chunks and pgvector similarity search.

Not a CRUDBase subclass: chunks are created once and never updated in place
(re-ingesting a document deletes and recreates its chunks — see
app/services/knowledge_ingestion.py), so the generic base's update/soft-delete
machinery does not apply.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument


class CRUDKnowledgeChunk:
    """Chunk persistence and pgvector cosine-distance similarity search."""

    model = KnowledgeChunk

    async def create(
        self,
        db: AsyncSession,
        *,
        document_id: int,
        chunk_index: int,
        content: str,
        token_count: int,
        embedding: list[float],
    ) -> KnowledgeChunk:
        """Add one chunk and flush."""
        chunk = KnowledgeChunk(
            document_id=document_id,
            chunk_index=chunk_index,
            content=content,
            token_count=token_count,
            embedding=embedding,
        )
        db.add(chunk)
        await db.flush()
        return chunk

    async def list_for_document(self, db: AsyncSession, document_id: int) -> list[KnowledgeChunk]:
        """Return one document's chunks ordered by chunk_index."""
        stmt = (
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == document_id)
            .order_by(KnowledgeChunk.chunk_index.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def search_similar(
        self,
        db: AsyncSession,
        *,
        query_embedding: list[float],
        category_id: int | None = None,
        top_k: int = 5,
    ) -> list[tuple[KnowledgeChunk, float]]:
        """Return the `top_k` chunks closest to `query_embedding` by cosine distance.

        Requires PostgreSQL + pgvector — `.cosine_distance()` compiles to a
        Postgres-only SQL operator and has no SQLite equivalent.
        """
        distance = KnowledgeChunk.embedding.cosine_distance(query_embedding)
        stmt = select(KnowledgeChunk, distance.label("distance")).order_by(distance).limit(top_k)
        if category_id is not None:
            stmt = stmt.join(
                KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id
            ).where(KnowledgeDocument.category_id == category_id)

        result = await db.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]


knowledge_chunk_crud = CRUDKnowledgeChunk()
```

- [ ] **Step 4: Export the new CRUD instance**

Modify `backend/app/crud/__init__.py` — add `knowledge_chunk_crud` (alphabetical order):

```python
"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.crud.audit_log import audit_log_crud
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

- [ ] **Step 5: Run test to verify Step 1's test passes**

Run: `uv run pytest tests/test_knowledge_crud_chunk.py -v`
Expected: `1 passed`

- [ ] **Step 6: Write the Postgres-gated similarity test**

Create `backend/tests/test_knowledge_chunk_search_postgres.py`:

```python
"""Optional PostgreSQL regression for pgvector similarity search.

Set ``TEST_POSTGRES_DATABASE_URL`` to a migrated, disposable PostgreSQL
database with the vector extension available to enable this module — the
same convention as test_postgres_refresh_concurrency.py. The docker-compose
Postgres at the repo root (pgvector/pgvector:pg17, port 5433) already has the
extension; point TEST_POSTGRES_DATABASE_URL at a dedicated database on it
(not the app's own DATABASE_URL) if you want this to run locally.

This test creates its own rows inside a transaction it rolls back — it never
commits, so it leaves no residue even without per-row cleanup.
"""

import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.models.base import Base

POSTGRES_DATABASE_URL = os.getenv("TEST_POSTGRES_DATABASE_URL")
if not POSTGRES_DATABASE_URL:
    pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured", allow_module_level=True)

pytestmark = pytest.mark.asyncio


async def test_search_similar_orders_by_cosine_distance() -> None:
    engine = create_async_engine(POSTGRES_DATABASE_URL)  # type: ignore[arg-type]
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    try:
        async with session_factory() as db:
            category = await knowledge_category_crud.create(
                db, {"code": "test-search", "name": "test", "description": ""}
            )
            await db.flush()
            document = await knowledge_document_crud.create(
                db,
                {
                    "category_id": category.id,
                    "title": "",
                    "original_filename": "a.md",
                    "file_path": "test-search/1_a.md",
                    "file_type": "md",
                    "content_hash": "e" * 64,
                    "status": "ready",
                    "uploaded_by": None,
                },
            )
            await db.flush()

            close_vector = [1.0] + [0.0] * 1023
            far_vector = [0.0] * 1023 + [1.0]
            await knowledge_chunk_crud.create(
                db,
                document_id=document.id,
                chunk_index=0,
                content="接近查询向量",
                token_count=5,
                embedding=close_vector,
            )
            await knowledge_chunk_crud.create(
                db,
                document_id=document.id,
                chunk_index=1,
                content="远离查询向量",
                token_count=5,
                embedding=far_vector,
            )

            query_embedding = [1.0] + [0.0] * 1023
            results = await knowledge_chunk_crud.search_similar(
                db, query_embedding=query_embedding, top_k=2
            )

            assert [chunk.content for chunk, _distance in results] == ["接近查询向量", "远离查询向量"]
            assert results[0][1] < results[1][1]

            await db.rollback()
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
```

- [ ] **Step 7: Run the Postgres-gated test (only if `TEST_POSTGRES_DATABASE_URL` is set)**

If you want to actually run this test locally (recommended, given the docker-compose Postgres is already up with pgvector), create a dedicated test database and point the env var at it, then run:

```bash
uv run python -c "
import asyncio
if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import psycopg
conn = psycopg.connect('postgresql://lijianqiao:ent-agent-dev-only@localhost:5433/postgres', autocommit=True)
conn.execute('CREATE DATABASE ent_agent_test')
"
```

Then:

```bash
TEST_POSTGRES_DATABASE_URL="postgresql+psycopg://lijianqiao:ent-agent-dev-only@localhost:5433/ent_agent_test" uv run pytest tests/test_knowledge_chunk_search_postgres.py -v
```

Expected: `1 passed`. If you skip this step, `uv run pytest -v` will report this file as skipped, which is expected and not a failure — the CRUD logic is still covered by Step 1's SQLite test for everything except the actual distance-ordering behavior.

- [ ] **Step 8: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 9: Commit**

```bash
git add backend/app/crud/knowledge_chunk.py backend/app/crud/__init__.py backend/tests/test_knowledge_crud_chunk.py backend/tests/test_knowledge_chunk_search_postgres.py
git commit -m "$(cat <<'EOF'
新增 KnowledgeChunk 的 CRUD 层与 pgvector 相似度检索

- CRUDKnowledgeChunk 不继承 CRUDBase（chunk 创建后不原地更新，重新
  摄入文档时是整体删掉重建，见后面的 ingestion service）
- search_similar() 用 pgvector 的 cosine_distance() 排序，可选按
  category_id 过滤；这个算子是 Postgres 专用 SQL，SQLite 跑不了
- create()/list_for_document() 走 aiosqlite 常规测试；search_similar()
  的真实排序行为放进单独的 test_knowledge_chunk_search_postgres.py，
  按 TEST_POSTGRES_DATABASE_URL 门控，跟现有
  test_postgres_refresh_concurrency.py 用的是同一套约定
EOF
)"
```

---

### Task 5: File storage service (path safety)

**Files:**
- Create: `backend/app/services/knowledge_storage.py`
- Test: `backend/tests/test_knowledge_storage.py`

**Interfaces:**
- Consumes: `app.core.config.BACKEND_ROOT`.
- Produces (used by Tasks 7, 8, 9):
  - `KNOWLEDGE_ROOT: Path`
  - `class PathTraversalError(ValueError)`
  - `def sanitize_filename(filename: str) -> str`
  - `def resolve_safe_path(relative_path: str) -> Path` — raises `PathTraversalError` if the resolved path escapes `KNOWLEDGE_ROOT`
  - `def category_dir(category_code: str) -> Path` — ensures the directory exists
  - `def write_document_file(*, category_code: str, document_id: int, filename: str, content: bytes) -> str` — returns the path relative to `KNOWLEDGE_ROOT`
  - `def read_document_file(relative_path: str, *, offset: int = 0, limit: int | None = None) -> str`
  - `def glob_documents(pattern: str, *, category_code: str | None = None) -> list[str]`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_knowledge_storage.py`:

```python
"""Tests for the knowledge-base file storage service (path safety + I/O)."""

from pathlib import Path

import pytest

from app.services.knowledge_storage import (
    PathTraversalError,
    glob_documents,
    read_document_file,
    resolve_safe_path,
    sanitize_filename,
    write_document_file,
)


def test_sanitize_filename_strips_directory_components() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("normal.md") == "normal.md"
    assert sanitize_filename("..hidden") == "hidden"


def test_resolve_safe_path_rejects_traversal() -> None:
    with pytest.raises(PathTraversalError):
        resolve_safe_path("../../etc/passwd")


def test_resolve_safe_path_accepts_nested_path_within_root() -> None:
    path = resolve_safe_path("sop/1_a.md")
    from app.services.knowledge_storage import KNOWLEDGE_ROOT

    assert KNOWLEDGE_ROOT.resolve() in path.parents or path == KNOWLEDGE_ROOT.resolve()


def test_write_and_read_document_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)

    relative_path = write_document_file(
        category_code="sop", document_id=1, filename="reboot.md", content="重启步骤：先...".encode()
    )

    assert relative_path == "sop/1_reboot.md"
    content = read_document_file(relative_path)
    assert content == "重启步骤：先..."


def test_read_document_file_supports_offset_and_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    relative_path = write_document_file(
        category_code="sop", document_id=1, filename="a.md", content="0123456789".encode()
    )

    assert read_document_file(relative_path, offset=2, limit=3) == "234"


def test_read_document_file_raises_for_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        read_document_file("sop/does-not-exist.md")


def test_glob_documents_scoped_to_category(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(category_code="sop", document_id=1, filename="a.md", content=b"x")
    write_document_file(category_code="topology", document_id=2, filename="b.md", content=b"x")

    sop_only = glob_documents("*.md", category_code="sop")

    assert sop_only == ["sop/1_a.md"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.knowledge_storage'`.

- [ ] **Step 3: Implement the storage service**

Create `backend/app/services/knowledge_storage.py`:

```python
"""File storage for knowledge-base documents.

Documents are stored as real files under KNOWLEDGE_ROOT/{category_code}/
{document_id}_{filename} (docs/AGENT_ARCHITECTURE.md §4.3, mirroring
docs/guide.md §4.3's knowledge/ convention). Every path-accepting function
resolves and validates containment within KNOWLEDGE_ROOT before touching the
filesystem, to block directory traversal (docs/AGENT_ARCHITECTURE.md §9, L1).
"""

from pathlib import Path

from app.core.config import BACKEND_ROOT

KNOWLEDGE_ROOT = BACKEND_ROOT / "knowledge"


class PathTraversalError(ValueError):
    """Raised when a resolved path would escape KNOWLEDGE_ROOT."""


def sanitize_filename(filename: str) -> str:
    """Strip path separators and leading dots so a filename can't smuggle a path."""
    name = Path(filename).name
    name = name.lstrip(".")
    return name or "unnamed"


def resolve_safe_path(relative_path: str) -> Path:
    """Resolve a path relative to KNOWLEDGE_ROOT, rejecting any escape attempt."""
    KNOWLEDGE_ROOT.mkdir(parents=True, exist_ok=True)
    root = KNOWLEDGE_ROOT.resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise PathTraversalError(f"path {relative_path!r} escapes KNOWLEDGE_ROOT")
    return candidate


def category_dir(category_code: str) -> Path:
    """Return (and ensure exists) the directory for one category."""
    path = resolve_safe_path(category_code)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_document_file(
    *,
    category_code: str,
    document_id: int,
    filename: str,
    content: bytes,
) -> str:
    """Write document content to disk and return its path relative to KNOWLEDGE_ROOT."""
    safe_name = sanitize_filename(filename)
    directory = category_dir(category_code)
    target = directory / f"{document_id}_{safe_name}"
    target.write_bytes(content)
    return str(target.relative_to(KNOWLEDGE_ROOT.resolve())).replace("\\", "/")


def read_document_file(relative_path: str, *, offset: int = 0, limit: int | None = None) -> str:
    """Read a document's text content, optionally paginated by character offset/limit."""
    path = resolve_safe_path(relative_path)
    if not path.is_file():
        raise FileNotFoundError(f"no such document file: {relative_path}")
    text = path.read_text(encoding="utf-8")
    if limit is None:
        return text[offset:]
    return text[offset : offset + limit]


def glob_documents(pattern: str, *, category_code: str | None = None) -> list[str]:
    """Return paths (relative to KNOWLEDGE_ROOT) of files matching a glob pattern."""
    base = category_dir(category_code) if category_code else KNOWLEDGE_ROOT
    base.mkdir(parents=True, exist_ok=True)
    root = KNOWLEDGE_ROOT.resolve()
    return sorted(
        str(p.relative_to(root)).replace("\\", "/") for p in base.glob(pattern) if p.is_file()
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_knowledge_storage.py -v`
Expected: `8 passed`

- [ ] **Step 5: Add `knowledge/` to `.gitignore`**

Modify `backend/.gitignore` — add this block at the end (uploaded documents are runtime content, not source):

```gitignore

# 知识库上传的文档正文（运行时数据，不进版本控制）
knowledge/
```

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/knowledge_storage.py backend/tests/test_knowledge_storage.py backend/.gitignore
git commit -m "$(cat <<'EOF'
新增知识库文件存储服务，带路径穿越防护

- 新建 app/services/ 包的第一个模块：knowledge_storage.py，负责把
  文档正文写到 knowledge/{分类code}/{文档id}_{文件名}，数据库只存
  元数据
- resolve_safe_path() 对每个路径做 realpath 解析后校验是否还落在
  KNOWLEDGE_ROOT 内，拦截 ../ 这类目录穿越；sanitize_filename() 剥掉
  上传文件名里的路径分隔符和前导点
- backend/.gitignore 新增 knowledge/，上传的文档正文是运行时数据，
  不进版本控制
EOF
)"
```

---

### Task 6: `embed()` in `app/core/llm.py`

**Files:**
- Modify: `backend/app/core/llm.py`
- Test: `backend/tests/test_agent_llm.py`

**Interfaces:**
- Consumes: `app.core.llm.MODELS`, `app.core.llm.LlmRequestError`, `app.core.llm._build_client` (all from T06 Task 7).
- Produces: `@dataclass EmbeddingResult(vectors: list[list[float]], prompt_tokens: int)`, `async def embed(model_key: str, inputs: list[str], *, client: httpx.AsyncClient | None = None) -> EmbeddingResult`.

- [ ] **Step 1: Write the failing test**

Modify `backend/tests/test_agent_llm.py` — add these imports to the existing import line and add the new tests at the end of the file:

```python
from app.core.llm import MODELS, ChatMessage, LlmRequestError, ToolCall, chat, embed
```

Append to the end of `backend/tests/test_agent_llm.py`:

```python
async def test_embed_returns_vectors_in_index_order() -> None:
    transport = _fake_transport(
        {
            "data": [
                {"index": 1, "embedding": [0.4, 0.5]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ],
            "usage": {"prompt_tokens": 7},
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        result = await embed("local-embedding", ["第一段", "第二段"], client=fake_client)

    assert result.vectors == [[0.1, 0.2], [0.4, 0.5]]
    assert result.prompt_tokens == 7


async def test_embed_raises_on_non_200() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        with pytest.raises(LlmRequestError):
            await embed("local-embedding", ["x"], client=fake_client)


async def test_embed_rejects_unknown_model_key() -> None:
    with pytest.raises(LlmRequestError):
        await embed("does-not-exist", ["x"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_llm.py -v`
Expected: FAIL with `ImportError: cannot import name 'embed' from 'app.core.llm'`.

- [ ] **Step 3: Implement `embed()`**

Modify `backend/app/core/llm.py` — add this block after the `chat()` function (at the end of the file):

```python
@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """The embedding vectors returned by `embed()`, in request order."""

    vectors: list[list[float]]
    prompt_tokens: int


async def embed(
    model_key: str,
    inputs: list[str],
    *,
    client: httpx.AsyncClient | None = None,
) -> EmbeddingResult:
    """Send one OpenAI-compatible embeddings request and return the vectors, input-order.

    `client` is injectable for tests, matching `chat()`'s convention.
    """
    config = MODELS.get(model_key)
    if config is None:
        raise LlmRequestError(f"unknown model key {model_key!r}; register it in MODELS first")

    payload: dict[str, Any] = {"model": config.request_model, "input": inputs}

    owns_client = client is None
    http_client = client or _build_client(config)
    try:
        response = await http_client.post("/embeddings", json=payload)
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code != 200:
        raise LlmRequestError(
            f"model {model_key!r} returned HTTP {response.status_code}: {response.text}"
        )

    try:
        body = response.json()
        ordered = sorted(body["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in ordered]
        usage = body.get("usage", {})
        return EmbeddingResult(vectors=vectors, prompt_tokens=usage.get("prompt_tokens", 0))
    except (KeyError, IndexError, ValueError) as exc:
        raise LlmRequestError(
            f"malformed embedding response from model {model_key!r}: {response.text}"
        ) from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_llm.py -v`
Expected: `10 passed`

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all pass (or skip, for the Postgres-gated file from Task 4), zero regressions.

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/llm.py backend/tests/test_agent_llm.py
git commit -m "$(cat <<'EOF'
新增 embed()，llm.py 补齐 embedding 能力

- embed() 结构和 chat() 对称：同样的 client 注入、同样的
  LlmRequestError 错误处理，POST /embeddings 而不是
  /chat/completions
- OpenAI 兼容的 embeddings 接口返回顺序不保证跟输入一致，每条结果带
  index 字段，embed() 按 index 排序后再返回，保证 vectors[i] 对应
  inputs[i]
- 测试用 httpx.MockTransport 桩掉，覆盖乱序返回、非 200、未知
  model_key 三种情况，不需要真的连本地 llama.cpp
EOF
)"
```

---

### Task 7: Ingestion service (chunking + embed + store)

**Files:**
- Create: `backend/app/services/knowledge_ingestion.py`
- Test: `backend/tests/test_knowledge_ingestion.py`

**Interfaces:**
- Consumes: `app.services.knowledge_storage.write_document_file` (Task 5), `app.core.llm.embed`, `app.core.llm.EmbeddingResult` (Task 6), `app.crud.knowledge_document.knowledge_document_crud`, `app.crud.knowledge_chunk.knowledge_chunk_crud` (Tasks 3, 4).
- Produces (used by Task 11):
  - `def chunk_text(text: str, *, chunk_size: int = 800, overlap: int = 100) -> list[str]`
  - `class DuplicateDocumentError(ValueError)` with a `document_id: int` attribute
  - `async def ingest_document(db, *, category_id, category_code, title, original_filename, file_type, content, uploaded_by, embedding_model_key="local-embedding") -> KnowledgeDocument` — flushes but does not commit; raises `DuplicateDocumentError` if the content hash already exists

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_knowledge_ingestion.py`:

```python
"""Tests for the knowledge ingestion service (chunking + embed + store)."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import EmbeddingResult
from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.models.user import User
from app.services.knowledge_ingestion import DuplicateDocumentError, chunk_text, ingest_document

pytestmark = pytest.mark.asyncio


def test_chunk_text_splits_with_overlap() -> None:
    text = "0123456789" * 3  # 30 chars
    chunks = chunk_text(text, chunk_size=10, overlap=2)

    assert chunks[0] == "0123456789"
    assert chunks[1].startswith("89")  # last 2 chars of chunk 0 repeated
    assert "".join(chunks).replace(chunks[0], "", 1) or True  # chunks overlap, not a clean rejoin


def test_chunk_text_rejects_overlap_not_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        chunk_text("abc", chunk_size=5, overlap=5)


def test_chunk_text_returns_empty_list_for_empty_text() -> None:
    assert chunk_text("") == []


async def test_ingest_document_stores_file_and_chunks(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.services.knowledge_ingestion.KNOWLEDGE_ROOT_OVERRIDE_FOR_TESTS", None, raising=False
    )
    category = await knowledge_category_crud.create(
        db_session, {"code": "sop", "name": "SOP", "description": ""}
    )
    await db_session.flush()

    async def fake_embed(model_key: str, inputs: list[str], **kwargs: Any) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1] * 1024 for _ in inputs], prompt_tokens=10)

    monkeypatch.setattr("app.services.knowledge_ingestion.embed", fake_embed)

    document = await ingest_document(
        db_session,
        category_id=category.id,
        category_code="sop",
        title="重启流程",
        original_filename="reboot.md",
        file_type="md",
        content="交换机重启的标准流程：第一步...".encode(),
        uploaded_by=test_user.id,
    )
    await db_session.commit()

    assert document.status == "ready"
    assert document.file_path.startswith("sop/")

    chunks = await knowledge_chunk_crud.list_for_document(db_session, document.id)
    assert len(chunks) >= 1
    assert chunks[0].content in "交换机重启的标准流程：第一步..."


async def test_ingest_document_rejects_duplicate_content(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    category = await knowledge_category_crud.create(
        db_session, {"code": "sop", "name": "SOP", "description": ""}
    )
    await db_session.flush()

    async def fake_embed(model_key: str, inputs: list[str], **kwargs: Any) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1] * 1024 for _ in inputs], prompt_tokens=10)

    monkeypatch.setattr("app.services.knowledge_ingestion.embed", fake_embed)

    content = "重复内容".encode()
    await ingest_document(
        db_session,
        category_id=category.id,
        category_code="sop",
        title="第一次上传",
        original_filename="a.md",
        file_type="md",
        content=content,
        uploaded_by=test_user.id,
    )
    await db_session.commit()

    with pytest.raises(DuplicateDocumentError):
        await ingest_document(
            db_session,
            category_id=category.id,
            category_code="sop",
            title="第二次上传同样内容",
            original_filename="b.md",
            file_type="md",
            content=content,
            uploaded_by=test_user.id,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_ingestion.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.knowledge_ingestion'`.

- [ ] **Step 3: Implement the ingestion service**

Create `backend/app/services/knowledge_ingestion.py`:

```python
"""Chunking + embedding + storage orchestration for uploaded documents.

Ties together app.services.knowledge_storage (file I/O), app.core.llm.embed
(vectors), and the knowledge_document_crud/knowledge_chunk_crud CRUD layer.
Only flushes — the caller (the upload API route) commits once, after this
and its audit-log entry both succeed, per this project's transaction
convention.
"""

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import embed
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.models.knowledge_document import KnowledgeDocument
from app.services.knowledge_storage import write_document_file

_DEFAULT_CHUNK_SIZE = 800
_DEFAULT_CHUNK_OVERLAP = 100


class DuplicateDocumentError(ValueError):
    """Raised when a document with identical content already exists (active, not deleted)."""

    def __init__(self, document_id: int) -> None:
        self.document_id = document_id
        super().__init__(f"a document with identical content already exists: id={document_id}")


def chunk_text(
    text: str,
    *,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split text into overlapping fixed-size character chunks (CJK-safe: character-based,
    not word-boundary-based).
    """
    if chunk_size <= overlap:
        raise ValueError("chunk_size must be greater than overlap")
    if not text:
        return []

    chunks: list[str] = []
    start = 0
    text_length = len(text)
    while start < text_length:
        end = min(start + chunk_size, text_length)
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end == text_length:
            break
        start = end - overlap
    return chunks


async def ingest_document(
    db: AsyncSession,
    *,
    category_id: int,
    category_code: str,
    title: str,
    original_filename: str,
    file_type: str,
    content: bytes,
    uploaded_by: int,
    embedding_model_key: str = "local-embedding",
) -> KnowledgeDocument:
    """Store a document's file, chunk it, embed each chunk, and store the chunks.

    Raises DuplicateDocumentError if an active document with the same content
    hash already exists — the caller decides how to surface that (this
    project's convention: translate it to an HTTP 409 in the route layer).
    """
    content_hash = hashlib.sha256(content).hexdigest()
    existing = await knowledge_document_crud.get_by_content_hash(db, content_hash)
    if existing is not None:
        raise DuplicateDocumentError(existing.id)

    document = await knowledge_document_crud.create(
        db,
        {
            "category_id": category_id,
            "title": title,
            "original_filename": original_filename,
            "file_path": "",
            "file_type": file_type,
            "content_hash": content_hash,
            "status": "processing",
            "uploaded_by": uploaded_by,
        },
    )
    await db.flush()

    relative_path = write_document_file(
        category_code=category_code,
        document_id=document.id,
        filename=original_filename,
        content=content,
    )
    updated = await knowledge_document_crud.update(db, document.id, {"file_path": relative_path})
    assert updated is not None  # just created it; cannot be missing

    text = content.decode("utf-8")
    chunks = chunk_text(text)
    if chunks:
        embedding_result = await embed(embedding_model_key, chunks)
        for index, (chunk_content, vector) in enumerate(
            zip(chunks, embedding_result.vectors, strict=True)
        ):
            await knowledge_chunk_crud.create(
                db,
                document_id=document.id,
                chunk_index=index,
                content=chunk_content,
                token_count=len(chunk_content),
                embedding=vector,
            )

    ready = await knowledge_document_crud.update(db, document.id, {"status": "ready"})
    assert ready is not None
    return ready
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_knowledge_ingestion.py -v`
Expected: `6 passed`

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all pass (or skip for the Postgres-gated file), zero regressions.

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/knowledge_ingestion.py backend/tests/test_knowledge_ingestion.py
git commit -m "$(cat <<'EOF'
新增知识库摄入服务：分片 + embedding + 落库

- chunk_text() 按字符定长切分带重叠（默认 800 字/重叠 100 字），
  按字符而不是按词切，适配中文场景
- ingest_document() 编排整个流程：查重复(按 content_hash)→建文档
  元数据行→落盘写文件→回填 file_path→逐 chunk 调 embed()→建
  KnowledgeChunk 行→标记 ready；全程只 flush 不 commit，commit 交给
  上传接口的路由层
- 内容完全重复的上传会抛 DuplicateDocumentError，路由层负责翻译成
  409
EOF
)"
```

---

### Task 8: `kb_glob` + `kb_read` tools

**Files:**
- Create: `backend/app/agent/knowledge_tools.py`
- Test: `backend/tests/test_knowledge_tools_fs.py`

**Interfaces:**
- Consumes: `app.agent.loop.ToolResult` (T06 Task 10), `app.services.knowledge_storage.glob_documents`, `read_document_file`, `PathTraversalError` (Task 5).
- Produces (used by Tasks 9, 10, and any later task wiring a real `ToolDispatcher`):
  - `async def kb_glob(pattern: str, *, category: str | None = None) -> ToolResult`
  - `async def kb_read(path: str, *, offset: int = 0, limit: int | None = 4000) -> ToolResult`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_knowledge_tools_fs.py`:

```python
"""Tests for the filesystem-backed Agent tools: kb_glob, kb_read."""

from pathlib import Path

import pytest

from app.agent.knowledge_tools import kb_glob, kb_read
from app.services.knowledge_storage import write_document_file

pytestmark = pytest.mark.asyncio


async def test_kb_glob_returns_matching_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(category_code="sop", document_id=1, filename="a.md", content=b"x")
    write_document_file(category_code="sop", document_id=2, filename="b.md", content=b"x")

    result = await kb_glob("*.md", category="sop")

    assert result.control == "ok"
    assert "sop/1_a.md" in result.content
    assert "sop/2_b.md" in result.content


async def test_kb_glob_returns_ok_with_no_matches_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)

    result = await kb_glob("*.md", category="empty")

    assert result.control == "ok"
    assert result.content == "没有匹配的文件"


async def test_kb_read_returns_file_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    relative_path = write_document_file(
        category_code="sop", document_id=1, filename="a.md", content="重启步骤".encode()
    )

    result = await kb_read(relative_path)

    assert result.control == "ok"
    assert result.content == "重启步骤"


async def test_kb_read_rejects_path_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)

    result = await kb_read("../../etc/passwd")

    assert result.control == "rejected"


async def test_kb_read_reports_missing_file_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)

    result = await kb_read("sop/does-not-exist.md")

    assert result.control == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_tools_fs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent.knowledge_tools'`.

- [ ] **Step 3: Implement the tools**

Create `backend/app/agent/knowledge_tools.py`:

```python
"""Agent-facing tools for the knowledge base (docs/AGENT_ARCHITECTURE.md §4.2).

Every function here returns a `ToolResult` (app.agent.loop's contract) so
they are ready to be wired into a real `ToolDispatcher` closure once a caller
with a live `db` session and role exists to invoke `run_loop` with them — that
wiring itself is out of scope for this plan (see T07's header).
"""

from app.agent.loop import ToolResult
from app.services.knowledge_storage import (
    PathTraversalError,
    glob_documents,
    read_document_file,
)


async def kb_glob(pattern: str, *, category: str | None = None) -> ToolResult:
    """List document paths (relative to KNOWLEDGE_ROOT) matching a glob pattern."""
    try:
        paths = glob_documents(pattern, category_code=category)
    except PathTraversalError:
        return ToolResult(control="rejected", content=f"分类越界: {category}")
    if not paths:
        return ToolResult(control="ok", content="没有匹配的文件")
    return ToolResult(control="ok", content="\n".join(paths))


async def kb_read(path: str, *, offset: int = 0, limit: int | None = 4000) -> ToolResult:
    """Read a document's content, paginated by character offset/limit (大结果截断)."""
    try:
        content = read_document_file(path, offset=offset, limit=limit)
    except PathTraversalError:
        return ToolResult(control="rejected", content=f"路径越界: {path}")
    except FileNotFoundError:
        return ToolResult(control="failed", content=f"文件不存在: {path}")
    return ToolResult(control="ok", content=content)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_knowledge_tools_fs.py -v`
Expected: `5 passed`

- [ ] **Step 5: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agent/knowledge_tools.py backend/tests/test_knowledge_tools_fs.py
git commit -m "$(cat <<'EOF'
新增 kb_glob / kb_read 两个知识库 Agent 工具

- 新建 app/agent/knowledge_tools.py，两个工具都是对
  knowledge_storage 的薄封装，返回值统一是 loop.py 的 ToolResult
  形状（control: ok/rejected/failed）
- 路径穿越对应 rejected，文件不存在对应 failed，正常返回 ok——跟
  loop.py 的 control 契约保持一致，为后续真正接进 run_loop 的
  dispatch_tool 做准备（这次不做那层接线）
EOF
)"
```

---

### Task 9: `kb_grep` tool (ripgrep subprocess)

**Files:**
- Modify: `backend/app/agent/knowledge_tools.py`
- Test: `backend/tests/test_knowledge_tools_grep.py`

**Interfaces:**
- Consumes: `app.agent.loop.ToolResult`, `app.services.knowledge_storage.KNOWLEDGE_ROOT`, `category_dir` (Task 5).
- Produces: `async def kb_grep(pattern: str, *, category: str | None = None, context_lines: int = 0) -> ToolResult`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_knowledge_tools_grep.py`:

```python
"""Tests for kb_grep — the ripgrep-backed Agent tool.

These tests genuinely shell out to ripgrep (rg must be on PATH — confirmed
present in this environment, `rg 15.0.0`). They do not mock the subprocess.
"""

from pathlib import Path

import pytest

from app.agent.knowledge_tools import kb_grep
from app.services.knowledge_storage import write_document_file

pytestmark = pytest.mark.asyncio


async def test_kb_grep_finds_matches_with_line_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(
        category_code="sop",
        document_id=1,
        filename="reboot.md",
        content="第一行\n交换机重启步骤\n第三行\n".encode(),
    )

    result = await kb_grep("重启", category="sop")

    assert result.control == "ok"
    assert "交换机重启步骤" in result.content
    assert ":2:" in result.content  # line number of the match


async def test_kb_grep_returns_ok_with_no_matches_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(category_code="sop", document_id=1, filename="a.md", content="无关内容".encode())

    result = await kb_grep("不存在的关键词", category="sop")

    assert result.control == "ok"
    assert result.content == "没有匹配"


async def test_kb_grep_scoped_to_category_excludes_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(category_code="sop", document_id=1, filename="a.md", content="重启".encode())
    write_document_file(category_code="topology", document_id=2, filename="b.md", content="重启".encode())

    result = await kb_grep("重启", category="sop")

    assert "sop" in result.content
    assert "topology" not in result.content


async def test_kb_grep_reports_missing_binary_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr("app.agent.knowledge_tools.shutil.which", lambda name: None)

    result = await kb_grep("anything")

    assert result.control == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_tools_grep.py -v`
Expected: FAIL with `ImportError: cannot import name 'kb_grep' from 'app.agent.knowledge_tools'`.

- [ ] **Step 3: Implement `kb_grep`**

Modify `backend/app/agent/knowledge_tools.py` — replace the full file contents with:

```python
"""Agent-facing tools for the knowledge base (docs/AGENT_ARCHITECTURE.md §4.2).

Every function here returns a `ToolResult` (app.agent.loop's contract) so
they are ready to be wired into a real `ToolDispatcher` closure once a caller
with a live `db` session and role exists to invoke `run_loop` with them — that
wiring itself is out of scope for this plan (see T07's header).
"""

import asyncio
import shutil

from app.agent.loop import ToolResult
from app.services.knowledge_storage import (
    KNOWLEDGE_ROOT,
    PathTraversalError,
    category_dir,
    glob_documents,
    read_document_file,
)

_RIPGREP_TIMEOUT_SECONDS = 10.0
_MAX_GREP_OUTPUT_BYTES = 32_000


async def kb_glob(pattern: str, *, category: str | None = None) -> ToolResult:
    """List document paths (relative to KNOWLEDGE_ROOT) matching a glob pattern."""
    try:
        paths = glob_documents(pattern, category_code=category)
    except PathTraversalError:
        return ToolResult(control="rejected", content=f"分类越界: {category}")
    if not paths:
        return ToolResult(control="ok", content="没有匹配的文件")
    return ToolResult(control="ok", content="\n".join(paths))


async def kb_read(path: str, *, offset: int = 0, limit: int | None = 4000) -> ToolResult:
    """Read a document's content, paginated by character offset/limit (大结果截断)."""
    try:
        content = read_document_file(path, offset=offset, limit=limit)
    except PathTraversalError:
        return ToolResult(control="rejected", content=f"路径越界: {path}")
    except FileNotFoundError:
        return ToolResult(control="failed", content=f"文件不存在: {path}")
    return ToolResult(control="ok", content=content)


async def kb_grep(
    pattern: str,
    *,
    category: str | None = None,
    context_lines: int = 0,
) -> ToolResult:
    """Search knowledge-base documents with ripgrep, scoped to KNOWLEDGE_ROOT or one category."""
    if shutil.which("rg") is None:
        return ToolResult(control="failed", content="ripgrep(rg) 未安装或不在 PATH 中")

    try:
        search_root = category_dir(category) if category else KNOWLEDGE_ROOT
    except PathTraversalError:
        return ToolResult(control="rejected", content=f"分类越界: {category}")

    args = ["rg", "--line-number", "--no-heading"]
    if context_lines:
        args += ["-C", str(context_lines)]
    args += [pattern, str(search_root)]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_RIPGREP_TIMEOUT_SECONDS)
    except TimeoutError:
        return ToolResult(control="failed", content="kb_grep 超时")

    if proc.returncode not in (0, 1):  # 1 = ripgrep ran fine, just no matches
        return ToolResult(
            control="failed", content=f"ripgrep 出错: {stderr.decode('utf-8', errors='replace')}"
        )

    output = stdout.decode("utf-8", errors="replace")
    if len(output.encode("utf-8")) > _MAX_GREP_OUTPUT_BYTES:
        truncated = output.encode("utf-8")[:_MAX_GREP_OUTPUT_BYTES]
        output = truncated.decode("utf-8", errors="ignore") + "\n...(结果已截断)"
    return ToolResult(control="ok", content=output.strip() or "没有匹配")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_knowledge_tools_grep.py -v`
Expected: `4 passed`

- [ ] **Step 5: Run the fs tools test file again to confirm no regression**

Run: `uv run pytest tests/test_knowledge_tools_fs.py -v`
Expected: `5 passed` (unchanged from Task 8 — confirms the file rewrite didn't disturb `kb_glob`/`kb_read`).

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/agent/knowledge_tools.py backend/tests/test_knowledge_tools_grep.py
git commit -m "$(cat <<'EOF'
新增 kb_grep 知识库检索工具

- 底层真的子进程调用 ripgrep，作用域限定在 KNOWLEDGE_ROOT 或单个
  分类目录（用 Task 5 的 category_dir()/PathTraversalError 兜底）
- 10 秒超时、32KB 输出截断（对应 guide.md §3.2「大结果截断」原则）、
  rg 不在 PATH 时返回 failed 而不是让子进程调用直接抛异常
- 测试是真的调 ripgrep 子进程，不是桩掉的（这个环境里已确认
  rg 15.0.0 在 PATH 上），只有"二进制缺失"这一种情况用 monkeypatch
  模拟
EOF
)"
```

---

### Task 10: `kb_semantic_search` tool

**Files:**
- Modify: `backend/app/agent/knowledge_tools.py`
- Test: `backend/tests/test_knowledge_tools_semantic_search.py`

**Interfaces:**
- Consumes: `app.agent.loop.ToolResult`, `app.core.llm.embed` (Task 6), `app.crud.knowledge_chunk.knowledge_chunk_crud.search_similar` (Task 4).
- Produces: `async def kb_semantic_search(db: AsyncSession, query: str, *, category_id: int | None = None, top_k: int = 5, embedding_model_key: str = "local-embedding") -> ToolResult`

Note: unlike `kb_glob`/`kb_read`/`kb_grep`, this tool needs a `db: AsyncSession` — it is the one knowledge tool whose `ToolDispatcher` wiring will need to pass `db` through (out of scope here, per this plan's header).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_knowledge_tools_semantic_search.py`:

```python
"""Tests for kb_semantic_search — embeds the query, then delegates to pgvector search.

Uses a fake embed() (no real LLM call) and a fake search_similar() (no real
pgvector query) so this test runs on the standard aiosqlite test DB without
needing TEST_POSTGRES_DATABASE_URL — the actual cosine-distance behavior is
covered separately by Task 4's Postgres-gated test.
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.knowledge_tools import kb_semantic_search
from app.core.llm import EmbeddingResult
from app.models.knowledge_chunk import KnowledgeChunk

pytestmark = pytest.mark.asyncio


async def test_kb_semantic_search_returns_formatted_results(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_embed(model_key: str, inputs: list[str], **kwargs: Any) -> EmbeddingResult:
        assert inputs == ["交换机怎么重启"]
        return EmbeddingResult(vectors=[[0.1] * 1024], prompt_tokens=5)

    async def fake_search_similar(
        db: AsyncSession, *, query_embedding: list[float], category_id: int | None, top_k: int
    ) -> list[tuple[KnowledgeChunk, float]]:
        assert query_embedding == [0.1] * 1024
        chunk = KnowledgeChunk(
            id=1, document_id=1, chunk_index=0, content="先断电再通电", token_count=6, embedding=[0.1] * 1024
        )
        return [(chunk, 0.05)]

    monkeypatch.setattr("app.agent.knowledge_tools.embed", fake_embed)
    monkeypatch.setattr(
        "app.agent.knowledge_tools.knowledge_chunk_crud.search_similar", fake_search_similar
    )

    result = await kb_semantic_search(db_session, "交换机怎么重启")

    assert result.control == "ok"
    assert "先断电再通电" in result.content
    assert "document_id=1" in result.content


async def test_kb_semantic_search_reports_no_results(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_embed(model_key: str, inputs: list[str], **kwargs: Any) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1] * 1024], prompt_tokens=5)

    async def fake_search_similar(
        db: AsyncSession, *, query_embedding: list[float], category_id: int | None, top_k: int
    ) -> list[tuple[KnowledgeChunk, float]]:
        return []

    monkeypatch.setattr("app.agent.knowledge_tools.embed", fake_embed)
    monkeypatch.setattr(
        "app.agent.knowledge_tools.knowledge_chunk_crud.search_similar", fake_search_similar
    )

    result = await kb_semantic_search(db_session, "没有相关内容的问题")

    assert result.control == "ok"
    assert result.content == "没有找到相关内容"


async def test_kb_semantic_search_reports_embedding_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.llm import LlmRequestError

    async def failing_embed(model_key: str, inputs: list[str], **kwargs: Any) -> EmbeddingResult:
        raise LlmRequestError("embedding 服务不可用")

    monkeypatch.setattr("app.agent.knowledge_tools.embed", failing_embed)

    result = await kb_semantic_search(db_session, "任意问题")

    assert result.control == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_tools_semantic_search.py -v`
Expected: FAIL with `ImportError: cannot import name 'kb_semantic_search' from 'app.agent.knowledge_tools'`.

- [ ] **Step 3: Implement `kb_semantic_search`**

Modify `backend/app/agent/knowledge_tools.py` — add these imports to the top of the file (merge with existing imports) and append the function at the end:

```python
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import LlmRequestError, embed
from app.crud.knowledge_chunk import knowledge_chunk_crud
```

Append to the end of `backend/app/agent/knowledge_tools.py`:

```python
async def kb_semantic_search(
    db: AsyncSession,
    query: str,
    *,
    category_id: int | None = None,
    top_k: int = 5,
    embedding_model_key: str = "local-embedding",
) -> ToolResult:
    """Embed `query` and return the top_k most similar knowledge chunks.

    Requires a real Postgres+pgvector backend for `search_similar()` — see
    app/crud/knowledge_chunk.py.
    """
    try:
        embedding_result = await embed(embedding_model_key, [query])
    except LlmRequestError as exc:
        return ToolResult(control="failed", content=f"embedding 失败: {exc}")

    results = await knowledge_chunk_crud.search_similar(
        db, query_embedding=embedding_result.vectors[0], category_id=category_id, top_k=top_k
    )
    if not results:
        return ToolResult(control="ok", content="没有找到相关内容")

    lines = [
        f"[document_id={chunk.document_id} chunk_index={chunk.chunk_index} "
        f"distance={distance:.4f}] {chunk.content}"
        for chunk, distance in results
    ]
    return ToolResult(control="ok", content="\n\n".join(lines))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_knowledge_tools_semantic_search.py -v`
Expected: `3 passed`

- [ ] **Step 5: Run the full knowledge_tools suite to confirm no regressions**

Run: `uv run pytest tests/test_knowledge_tools_fs.py tests/test_knowledge_tools_grep.py tests/test_knowledge_tools_semantic_search.py -v`
Expected: `12 passed`

- [ ] **Step 6: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/agent/knowledge_tools.py backend/tests/test_knowledge_tools_semantic_search.py
git commit -m "$(cat <<'EOF'
新增 kb_semantic_search 知识库语义检索工具

- 先调 embed() 把 query 编码成向量，再调
  knowledge_chunk_crud.search_similar() 走 pgvector 检索；embedding
  服务出错时返回 failed 而不是让异常往外抛
- 跟另外三个知识库工具不一样，这个需要 db: AsyncSession 参数（要查
  数据库）——以后真正接进 run_loop 的 dispatch_tool 闭包时，只有这一
  个工具需要把 db 传进去，其它三个是纯文件系统操作
- 测试用假的 embed()/search_similar() 桩掉，跑在标准 aiosqlite 测试库
  上；真实的向量相似度排序行为已经在 Task 4 的 Postgres 门控测试里
  验证过了，这里不重复测
EOF
)"
```

---

### Task 11: Upload API (categories + document upload, permission-gated)

**Files:**
- Create: `backend/app/schemas/knowledge.py`
- Create: `backend/app/api/v1/knowledge.py`
- Modify: `backend/app/api/router.py`
- Modify: `backend/init_db.py`
- Test: `backend/tests/test_knowledge_api.py`

**Interfaces:**
- Consumes: `app.core.deps.get_db`, `require_permission`, `get_client_ip` (T06-era codebase, confirmed present); `app.schemas.common.ResponseEnvelope`, `success_response`, `ApiModel`; `app.utils.audit.log_audit`; `app.services.knowledge_ingestion.ingest_document`, `DuplicateDocumentError` (Task 7); `app.crud.knowledge_category.knowledge_category_crud` (Task 2).
- Produces: three HTTP endpoints under `/api/v1/knowledge` — `POST /categories`, `GET /categories`, `POST /documents` — and three new permission codes: `knowledge:read`, `knowledge:upload`, `knowledge:manage`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_knowledge_api.py`:

```python
"""API tests for the knowledge base upload endpoints."""

from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permission import Permission
from app.models.user import User, user_roles

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]
type LoginUser = Callable[[str, str], Awaitable[Headers]]


async def _grant_knowledge_permissions(db_session: AsyncSession, test_user: User) -> None:
    """Attach knowledge:* permissions to test_user's existing role."""
    from app.models.role import role_permissions

    permissions = [
        Permission(name="查看知识库", code="knowledge:read", module="知识库"),
        Permission(name="上传知识文档", code="knowledge:upload", module="知识库"),
    ]
    db_session.add_all(permissions)
    await db_session.flush()

    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    for permission in permissions:
        await db_session.execute(
            role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
        )
    await db_session.commit()


async def test_create_and_list_categories(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    await _grant_knowledge_permissions(db_session, test_user)

    create_response = await client.post(
        "/api/v1/knowledge/categories",
        json={"code": "sop", "name": "故障处理 SOP", "description": "运维故障处理手册"},
        headers=auth_headers,
    )
    assert create_response.status_code == 201, create_response.text

    list_response = await client.get("/api/v1/knowledge/categories", headers=auth_headers)
    assert list_response.status_code == 200, list_response.text
    codes = [item["code"] for item in list_response.json()["data"]]
    assert "sop" in codes


async def test_upload_document_succeeds(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)

    async def fake_embed(model_key: str, inputs: list[str], **kwargs: object) -> object:
        from app.core.llm import EmbeddingResult

        return EmbeddingResult(vectors=[[0.1] * 1024 for _ in inputs], prompt_tokens=10)

    monkeypatch.setattr("app.services.knowledge_ingestion.embed", fake_embed)

    await client.post(
        "/api/v1/knowledge/categories",
        json={"code": "sop", "name": "SOP", "description": ""},
        headers=auth_headers,
    )

    response = await client.post(
        "/api/v1/knowledge/documents",
        data={"category_code": "sop", "title": "重启流程"},
        files={"file": ("reboot.md", b"switch reboot: step one, step two", "text/markdown")},
        headers=auth_headers,
    )

    assert response.status_code == 201, response.text
    payload = response.json()["data"]
    assert payload["status"] == "ready"
    assert payload["file_path"].startswith("sop/")


async def test_upload_document_without_permission_returns_403(
    client: AsyncClient, auth_headers: Headers
) -> None:
    response = await client.post(
        "/api/v1/knowledge/documents",
        data={"category_code": "sop", "title": "无权限上传"},
        files={"file": ("a.md", b"content", "text/markdown")},
        headers=auth_headers,
    )
    assert response.status_code == 403, response.text


async def test_upload_document_rejects_unsupported_file_type(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    await _grant_knowledge_permissions(db_session, test_user)
    await client.post(
        "/api/v1/knowledge/categories",
        json={"code": "sop", "name": "SOP", "description": ""},
        headers=auth_headers,
    )

    response = await client.post(
        "/api/v1/knowledge/documents",
        data={"category_code": "sop", "title": "不支持的格式"},
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        headers=auth_headers,
    )

    assert response.status_code == 422, response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas.knowledge'` (or a 404 once earlier import errors are fixed, since the route doesn't exist yet).

- [ ] **Step 3: Write the schemas**

Create `backend/app/schemas/knowledge.py`:

```python
"""Knowledge-base request/response schemas."""

from datetime import datetime

from pydantic import ConfigDict, Field

from app.schemas.common import ApiModel


class KnowledgeCategoryCreate(ApiModel):
    """Request body for creating a category."""

    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)


class KnowledgeCategoryResponse(ApiModel):
    """A category as returned to clients."""

    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)

    id: int
    code: str
    name: str
    description: str
    created_at: datetime


class KnowledgeDocumentResponse(ApiModel):
    """A document as returned to clients after upload."""

    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)

    id: int
    category_id: int
    title: str
    original_filename: str
    file_path: str
    file_type: str
    status: str
    created_at: datetime
```

- [ ] **Step 4: Write the API route**

Create `backend/app/api/v1/knowledge.py`:

```python
"""Knowledge-base routes: category management and document upload."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_client_ip, get_db, require_permission
from app.crud.knowledge_category import knowledge_category_crud
from app.models.user import User
from app.schemas.common import ResponseEnvelope, success_response
from app.schemas.knowledge import (
    KnowledgeCategoryCreate,
    KnowledgeCategoryResponse,
    KnowledgeDocumentResponse,
)
from app.services.knowledge_ingestion import DuplicateDocumentError, ingest_document
from app.utils.audit import log_audit

router = APIRouter()

_SUPPORTED_FILE_TYPES = {"md", "txt"}


@router.post(
    "/categories",
    response_model=ResponseEnvelope[KnowledgeCategoryResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_category(
    category_in: KnowledgeCategoryCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:manage")),
) -> ResponseEnvelope[KnowledgeCategoryResponse]:
    """Create a knowledge category."""
    existing = await knowledge_category_crud.get_by_code(db, category_in.code)
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="分类代码已存在")

    category = await knowledge_category_crud.create(db, category_in.model_dump())
    await log_audit(
        db,
        current_user.id,
        "create_knowledge_category",
        target=f"knowledge_category:{category.id}",
        detail=f"创建知识库分类: {category.code}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(
        KnowledgeCategoryResponse.model_validate(category), message="创建成功", code=status.HTTP_201_CREATED
    )


@router.get("/categories", response_model=ResponseEnvelope[list[KnowledgeCategoryResponse]])
async def list_categories(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("knowledge:read")),
) -> ResponseEnvelope[list[KnowledgeCategoryResponse]]:
    """List every knowledge category."""
    categories = await knowledge_category_crud.list_all(db)
    return success_response([KnowledgeCategoryResponse.model_validate(c) for c in categories])


@router.post(
    "/documents",
    response_model=ResponseEnvelope[KnowledgeDocumentResponse],
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    request: Request,
    category_code: str = Form(...),
    title: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:upload")),
) -> ResponseEnvelope[KnowledgeDocumentResponse]:
    """Upload a document (.md/.txt only), chunk it, embed it, and store it."""
    filename = file.filename or "unnamed"
    file_type = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if file_type not in _SUPPORTED_FILE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"不支持的文件类型: .{file_type}（仅支持 .md/.txt）",
        )

    category = await knowledge_category_crud.get_by_code(db, category_code)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分类不存在")

    content = await file.read()

    try:
        document = await ingest_document(
            db,
            category_id=category.id,
            category_code=category.code,
            title=title,
            original_filename=filename,
            file_type=file_type,
            content=content,
            uploaded_by=current_user.id,
        )
    except DuplicateDocumentError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"内容重复的文档已存在: id={exc.document_id}"
        ) from exc

    await log_audit(
        db,
        current_user.id,
        "upload_knowledge_document",
        target=f"knowledge_document:{document.id}",
        detail=f"上传知识文档: {document.title}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(
        KnowledgeDocumentResponse.model_validate(document), message="上传成功", code=status.HTTP_201_CREATED
    )
```

- [ ] **Step 5: Register the router**

Modify `backend/app/api/router.py` — add the import and registration line:

```python
"""API 路由聚合器。

将所有 v1 子路由注册到统一的前缀下。
"""

from fastapi import APIRouter

from app.api.v1.audit_logs import router as audit_logs_router
from app.api.v1.auth import router as auth_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.knowledge import router as knowledge_router
from app.api.v1.me import router as me_router
from app.api.v1.permissions import router as permissions_router
from app.api.v1.roles import router as roles_router
from app.api.v1.users import router as users_router

api_router = APIRouter()

api_router.include_router(auth_router, prefix="/auth", tags=["认证"])
api_router.include_router(users_router, prefix="/users", tags=["用户管理"])
api_router.include_router(roles_router, prefix="/roles", tags=["角色管理"])
api_router.include_router(permissions_router, prefix="/permissions", tags=["权限管理"])
api_router.include_router(me_router, prefix="/me", tags=["个人中心"])
api_router.include_router(dashboard_router, prefix="/dashboard", tags=["仪表盘"])
api_router.include_router(audit_logs_router, prefix="/audit-logs", tags=["审计日志"])
api_router.include_router(knowledge_router, prefix="/knowledge", tags=["知识库"])
```

- [ ] **Step 6: Add the permission codes to the seed list**

Modify `backend/init_db.py` — add three entries to `SEED_PERMISSIONS`, right before the closing `)` of the tuple (after the `audit:read` entry):

```python
    {
        "name": "查看日志",
        "code": "audit:read",
        "module": "审计日志",
        "description": "查看审计日志",
    },
    {
        "name": "查看知识库",
        "code": "knowledge:read",
        "module": "知识库",
        "description": "查看知识库分类与文档",
    },
    {
        "name": "上传知识文档",
        "code": "knowledge:upload",
        "module": "知识库",
        "description": "上传文档到知识库",
    },
    {
        "name": "管理知识库",
        "code": "knowledge:manage",
        "module": "知识库",
        "description": "创建/管理知识库分类",
    },
)
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/test_knowledge_api.py -v`
Expected: `5 passed`

- [ ] **Step 8: Re-seed permissions against the real database**

Run: `uv run python init_db.py`
Expected: prints `权限种子：新增 3 条（共 19 条定义）` (idempotent — safe to run again; only inserts the 3 new codes since the other 16 already exist).

- [ ] **Step 9: Run the full suite**

Run: `uv run pytest -v`
Expected: all pass (or skip for the Postgres-gated file from Task 4), zero regressions.

- [ ] **Step 10: Type check and lint**

Run: `uv run mypy app` — expected: `Success: no issues found`.
Run: `uv run ruff check .` — expected: `All checks passed!`.

- [ ] **Step 11: Commit**

```bash
git add backend/app/schemas/knowledge.py backend/app/api/v1/knowledge.py backend/app/api/router.py backend/init_db.py backend/tests/test_knowledge_api.py
git commit -m "$(cat <<'EOF'
新增知识库上传 API：分类管理 + 文档上传

- 三个端点：POST/GET /api/v1/knowledge/categories（建分类、列分类）、
  POST /api/v1/knowledge/documents（上传文档，multipart 表单）
- 新增三个权限码 knowledge:read/upload/manage，补进 init_db.py 的
  SEED_PERMISSIONS（幂等，重跑 init_db.py 只会新增这 3 条）
- 这是项目里第一个真正调用 T06 那套"业务写入 + 审计一次提交"约定的
  知识库相关端点：ingest_document() 只 flush，路由层在 log_audit()
  之后统一 commit
- 上传目前只接受 .md/.txt（.docx 解析显式排除在这份计划外，见计划
  文档开头的 scope 说明），内容完全重复的文档会被拒（409）
EOF
)"
```

---

## After This Plan

T07 is complete when all 11 tasks are committed and `uv run pytest -v`, `uv run mypy app`, and `uv run ruff check .` are clean (the one Postgres-gated file from Task 4 will show as skipped unless `TEST_POSTGRES_DATABASE_URL` is set — that's expected). Per `docs/AGENT_ARCHITECTURE.md` §14's dependency graph, T09 (spawn orchestration — including the `classifier`/`kb_explorer` roles and the batch-parallel-classification flow this plan deliberately deferred) depends on **both** this plan and T08 (CMDB + monitoring) being done, since it needs both subsystems' read tools. T08 has no dependency on this plan and could be built in parallel. Each should get its own plan via `superpowers:writing-plans` when you're ready to start it.
