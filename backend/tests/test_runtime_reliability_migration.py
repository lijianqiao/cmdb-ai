"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_runtime_reliability_migration.py
@DateTime: 2026-08-14
@Docs: Agent 运行时可靠性迁移的契约测试。
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "2026_08_14_1600-f2b4c6d8e013_agent_runtime_reliability.py"
)


class _FakeBatchOp:
    """模拟 batch_alter_table 上下文中的 batch_op。"""

    def __init__(self, parent: "_FakeOp", table_name: str) -> None:
        self._parent = parent
        self._table_name = table_name

    def add_column(self, column: sa.Column[Any]) -> None:
        self._parent.actions.append(("add_column", self._table_name, column.name))

    def create_foreign_key(
        self,
        constraint_name: str,
        referent_table: str,
        local_cols: list[str],
        remote_cols: list[str],
        **kwargs: object,
    ) -> None:
        self._parent.actions.append(
            (
                "create_foreign_key",
                self._table_name,
                constraint_name,
                referent_table,
                local_cols,
                remote_cols,
                kwargs,
            )
        )

    def create_index(
        self,
        index_name: str,
        columns: list[str],
        *,
        unique: bool = False,
    ) -> None:
        self._parent.actions.append(
            ("create_index", self._table_name, index_name, columns, unique)
        )


class _FakeBatchContext:
    """模拟 op.batch_alter_table 上下文管理器。"""

    def __init__(self, parent: "_FakeOp", table_name: str) -> None:
        self._batch_op = _FakeBatchOp(parent, table_name)

    def __enter__(self) -> _FakeBatchOp:
        return self._batch_op

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeOp:
    """记录 upgrade/downgrade 期间 Alembic 操作。"""

    def __init__(self) -> None:
        self.actions: list[tuple[str, ...]] = []

    @staticmethod
    def f(name: str) -> str:
        return name

    def batch_alter_table(
        self, table_name: str, schema: str | None = None
    ) -> _FakeBatchContext:
        return _FakeBatchContext(self, table_name)


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_runtime_reliability", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_reliability_migration_follows_current_head() -> None:
    """新迁移应紧跟 compaction 迁移并成为唯一 head。"""
    migration = _load_migration(MIGRATION_PATH)
    assert migration.revision == "f2b4c6d8e013"
    assert migration.down_revision == "c1a8e4b7d902"


def test_upgrade_adds_hitl_session_columns_and_foreign_key() -> None:
    """upgrade 应通过 batch_alter_table 添加 HITL/会话列及 resolved_by 外键。"""
    migration = _load_migration(MIGRATION_PATH)
    fake_op = _FakeOp()
    migration.op = fake_op

    migration.upgrade()

    hitl_columns = [
        action[2]
        for action in fake_op.actions
        if action[0] == "add_column" and action[1] == "hitl_proposals"
    ]
    assert hitl_columns == [
        "execution_started_at",
        "status_reason",
        "resolved_by_user_id",
        "resolved_at",
    ]

    session_columns = [
        action[2]
        for action in fake_op.actions
        if action[0] == "add_column" and action[1] == "agent_sessions"
    ]
    assert session_columns == ["active_turn_token", "active_turn_started_at"]

    foreign_keys = [
        action[2]
        for action in fake_op.actions
        if action[0] == "create_foreign_key"
    ]
    assert "fk_hitl_proposals_resolved_by_user_id_users" in foreign_keys


def test_downgrade_blocks_without_destructive_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """未显式允许破坏性降级时应拒绝执行 downgrade。"""
    migration = _load_migration(MIGRATION_PATH)

    def _empty_x_arguments(*, as_dictionary: bool = False) -> dict[str, str]:
        return {}

    monkeypatch.setattr(migration.context, "get_x_argument", _empty_x_arguments)

    with pytest.raises(RuntimeError, match="Destructive downgrade blocked"):
        migration.downgrade()
