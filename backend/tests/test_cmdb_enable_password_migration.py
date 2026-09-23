"""CMDB enable 口令迁移（P3c）的契约测试：只验证迁移发出的 DDL，不连数据库。"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa

VERSIONS_DIR = Path(__file__).parents[1] / "alembic" / "versions"
MIGRATION_PATH = VERSIONS_DIR / "2026_09_23_1500-d4a8c2e6f1b3_cmdb_enable_password.py"
PREVIOUS_HEAD = "b7e2d4f1c3a9"


class _FakeOp:
    """记录迁移实际发出的操作。"""

    def __init__(self) -> None:
        self.actions: list[tuple[Any, ...]] = []

    def add_column(self, table: str, column: sa.Column[Any]) -> None:
        default = column.server_default
        default_text = str(default.arg) if isinstance(default, sa.DefaultClause) else None
        self.actions.append(("add_column", table, column.name, column.nullable, default_text))

    def drop_column(self, table: str, column: str) -> None:
        self.actions.append(("drop_column", table, column))


class _FakeContext:
    def __init__(self, arguments: dict[str, str]) -> None:
        self._arguments = arguments

    def get_x_argument(self, *, as_dictionary: bool) -> dict[str, str]:
        return self._arguments


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cmdb_enable_password", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_follows_current_head_and_is_the_only_new_head() -> None:
    migration = _load()
    assert migration.down_revision == PREVIOUS_HEAD
    children_of_previous_head = [
        path.name
        for path in VERSIONS_DIR.glob("*.py")
        if f'down_revision: str | None = "{PREVIOUS_HEAD}"' in path.read_text(encoding="utf-8")
    ]
    assert children_of_previous_head == [MIGRATION_PATH.name]


def test_upgrade_adds_enable_type_defaulting_to_none_and_a_nullable_ciphertext() -> None:
    """已有资产一律按「没有 enable 口令」回填：升级后连接行为不变。"""
    migration = _load()
    fake_op = _FakeOp()
    migration.op = fake_op

    migration.upgrade()

    assert fake_op.actions == [
        ("add_column", "cmdb_assets", "enable_credential_type", False, "none"),
        ("add_column", "cmdb_assets", "enable_password_encrypted", True, None),
    ]


def test_downgrade_requires_explicit_destructive_opt_in() -> None:
    """回退会丢掉已登记的 enable 口令密文：必须显式确认。"""
    migration = _load()
    migration.op = _FakeOp()
    migration.context = _FakeContext({})

    with pytest.raises(RuntimeError, match="allow-destructive"):
        migration.downgrade()


def test_confirmed_downgrade_drops_both_columns() -> None:
    migration = _load()
    fake_op = _FakeOp()
    migration.op = fake_op
    migration.context = _FakeContext({"allow-destructive": "true"})

    migration.downgrade()

    assert fake_op.actions == [
        ("drop_column", "cmdb_assets", "enable_password_encrypted"),
        ("drop_column", "cmdb_assets", "enable_credential_type"),
    ]
