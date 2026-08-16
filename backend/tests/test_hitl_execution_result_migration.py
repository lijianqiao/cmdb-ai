"""HITL 执行结果表迁移的契约测试。"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "2026_08_15_1000-a7c9e2f4b681_hitl_execution_results.py"
)


class _FakeOp:
    """记录迁移实际发出的建表和建索引 DDL 操作。"""

    def __init__(self) -> None:
        self.actions: list[tuple[Any, ...]] = []

    @staticmethod
    def f(name: str) -> str:
        return name

    def create_table(self, table_name: str, *columns_and_constraints: object) -> None:
        self.actions.append(("create_table", table_name, columns_and_constraints))

    def create_index(
        self,
        index_name: str,
        table_name: str,
        columns: list[str],
        *,
        unique: bool = False,
    ) -> None:
        self.actions.append(("create_index", index_name, table_name, columns, unique))


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("hitl_execution_results", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_result_migration_follows_current_head() -> None:
    migration = _load_migration(MIGRATION_PATH)
    assert migration.revision == "a7c9e2f4b681"
    assert migration.down_revision == "f2b4c6d8e013"


def test_upgrade_creates_result_table_and_proposal_index() -> None:
    migration = _load_migration(MIGRATION_PATH)
    fake_op = _FakeOp()
    migration.op = fake_op

    migration.upgrade()

    create_table = next(action for action in fake_op.actions if action[0] == "create_table")
    assert create_table[1] == "hitl_execution_results"
    columns_and_constraints = create_table[2]
    proposal_column = next(
        item
        for item in columns_and_constraints
        if isinstance(item, sa.Column) and item.name == "proposal_id"
    )
    assert proposal_column.nullable is False
    foreign_key = next(
        item for item in columns_and_constraints if isinstance(item, sa.ForeignKeyConstraint)
    )
    assert foreign_key.ondelete == "CASCADE"
    assert any(isinstance(item, sa.UniqueConstraint) for item in columns_and_constraints)
    assert (
        "create_index",
        "ix_hitl_execution_results_proposal_id",
        "hitl_execution_results",
        ["proposal_id"],
        False,
    ) in fake_op.actions
