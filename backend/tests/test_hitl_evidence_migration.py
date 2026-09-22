"""HITL 证据保留迁移（R4）的契约测试：只验证迁移发出的 DDL/回填语句，不连数据库。"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa

VERSIONS_DIR = Path(__file__).parents[1] / "alembic" / "versions"
MIGRATION_PATH = VERSIONS_DIR / "2026_09_22_1000-e3a7c1f9b246_hitl_evidence_retention.py"


class _FakeOp:
    """记录迁移实际发出的操作。"""

    def __init__(self) -> None:
        self.actions: list[tuple[Any, ...]] = []

    def get_bind(self) -> object:
        return object()

    def drop_constraint(self, name: str, table: str, type_: str | None = None) -> None:
        self.actions.append(("drop_constraint", name, table, type_))

    def create_foreign_key(
        self,
        name: str,
        source: str,
        referent: str,
        local_cols: list[str],
        remote_cols: list[str],
        ondelete: str | None = None,
    ) -> None:
        self.actions.append(
            ("create_foreign_key", name, source, referent, tuple(local_cols), ondelete)
        )

    def add_column(self, table: str, column: sa.Column[Any]) -> None:
        self.actions.append(("add_column", table, column.name, column.nullable))

    def drop_column(self, table: str, column: str) -> None:
        self.actions.append(("drop_column", table, column))

    def execute(self, statement: object) -> None:
        self.actions.append(("execute", str(statement)))


class _FakeContext:
    def __init__(self, arguments: dict[str, str]) -> None:
        self._arguments = arguments

    def get_x_argument(self, *, as_dictionary: bool) -> dict[str, str]:
        return self._arguments


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hitl_evidence_retention", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_actions() -> list[tuple[Any, ...]]:
    migration = _load()
    fake_op = _FakeOp()
    migration.op = fake_op
    migration._session_fk_name = lambda bind: "hitl_proposals_session_id_fkey"
    migration.upgrade()
    return fake_op.actions


def test_migration_follows_current_head_and_is_the_only_new_head() -> None:
    migration = _load()
    assert migration.revision == "e3a7c1f9b246"
    assert migration.down_revision == "d0f5b8c4e236"
    children_of_previous_head = [
        path.name
        for path in VERSIONS_DIR.glob("*.py")
        if 'down_revision: str | None = "d0f5b8c4e236"' in path.read_text(encoding="utf-8")
    ]
    assert children_of_previous_head == [MIGRATION_PATH.name]


def test_session_foreign_key_becomes_restrict() -> None:
    """有提案的会话不能再被删除时级联带走证据。"""
    actions = _upgrade_actions()

    assert ("drop_constraint", "hitl_proposals_session_id_fkey", "hitl_proposals", "foreignkey") in actions
    assert (
        "create_foreign_key",
        "fk_hitl_proposals_session_id_agent_sessions",
        "hitl_proposals",
        "agent_sessions",
        ("session_id",),
        "RESTRICT",
    ) in actions


def test_evidence_columns_are_added_nullable() -> None:
    """旧记录没有这些事实，先允许为空，不拿当前值冒充历史。"""
    actions = _upgrade_actions()

    added = {action[2]: action[3] for action in actions if action[0] == "add_column"}
    assert added == {
        "requested_by_user_id": True,
        "approval_method": True,
        "evidence_snapshot": True,
    }
    assert (
        "create_foreign_key",
        "fk_hitl_proposals_requested_by_user_id_users",
        "hitl_proposals",
        "users",
        ("requested_by_user_id",),
        "SET NULL",
    ) in actions


def test_backfill_only_confirmed_facts() -> None:
    """申请人取会话所有者、审批方式按审计日志推出；历史快照一律不回填。"""
    statements = [action[1] for action in _upgrade_actions() if action[0] == "execute"]

    assert any("requested_by_user_id" in sql and "agent_sessions" in sql for sql in statements)
    assert any("approval_method" in sql and "audit_logs" in sql for sql in statements)
    assert not any("evidence_snapshot" in sql for sql in statements)


def test_downgrade_requires_explicit_destructive_opt_in() -> None:
    """回退会删掉证据列、恢复级联删除：必须显式确认，不能当常规回退。"""
    migration = _load()
    migration.op = _FakeOp()
    migration.context = _FakeContext({})

    with pytest.raises(RuntimeError, match="allow-destructive"):
        migration.downgrade()
