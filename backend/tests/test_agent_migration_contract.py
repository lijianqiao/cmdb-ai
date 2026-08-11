"""Behavior contract for the AgentRegistry Spawn-fields migration."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "2026_08_11_1830-d6a1b4c9f235_agent_registry_spawn_fields.py"
)


class _FakeMappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._rows)


class _FakeBind:
    def __init__(self, actions: list[tuple[str, object]]) -> None:
        self.actions = actions
        created_at = datetime(2026, 8, 10, tzinfo=UTC)
        self.rows = [
            {
                "child_id": "legacy-child",
                "created_at": created_at,
                "closed_at": created_at.replace(day=11),
                "budget": {"max_steps": 7, "extra": "drop-me"},
            },
            {
                "child_id": "legacy-open-child",
                "created_at": created_at.replace(day=9),
                "closed_at": None,
                "budget": None,
            },
        ]

    def execute(
        self, statement: sa.Executable, parameters: dict[str, object] | None = None
    ) -> _FakeResult:
        if parameters is None:
            self.actions.append(("select", statement))
            return _FakeResult(self.rows)
        self.actions.append(("update", parameters))
        return _FakeResult([])


class _FakeOp:
    def __init__(self) -> None:
        self.actions: list[tuple[str, object]] = []
        self.bind = _FakeBind(self.actions)

    def add_column(self, table_name: str, column: sa.Column[Any]) -> None:
        self.actions.append(("add_column", (table_name, column)))

    def get_bind(self) -> _FakeBind:
        return self.bind

    def alter_column(self, table_name: str, column_name: str, **kwargs: object) -> None:
        self.actions.append(("alter_column", (table_name, column_name, kwargs)))

    def create_index(
        self,
        index_name: str,
        table_name: str,
        columns: list[str],
        *,
        unique: bool,
    ) -> None:
        self.actions.append(("create_index", (index_name, table_name, columns, unique)))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_registry_spawn_fields", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_backfills_legacy_rows_before_enforcing_constraints() -> None:
    migration = _load_migration()
    fake_op = _FakeOp()
    migration.op = fake_op

    migration.upgrade()

    assert migration.down_revision == "c5f0a3b8e124"
    updates = [value for action, value in fake_op.actions if action == "update"]
    assert updates[0] == {
        "target_child_id": "legacy-child",
        "trace_id": "legacy-child",
        "role_version": "legacy",
        "status_changed_at": datetime(2026, 8, 11, tzinfo=UTC),
        "force_closed": False,
        "budget": {
            "max_steps": 7,
            "max_cost_usd": 1.0,
            "max_wall_time_seconds": 120.0,
            "steps_used": 0,
            "cost_used_usd": 0.0,
        },
    }
    assert updates[1] == {
        "target_child_id": "legacy-open-child",
        "trace_id": "legacy-open-child",
        "role_version": "legacy",
        "status_changed_at": datetime(2026, 8, 9, tzinfo=UTC),
        "force_closed": False,
        "budget": {
            "max_steps": 20,
            "max_cost_usd": 1.0,
            "max_wall_time_seconds": 120.0,
            "steps_used": 0,
            "cost_used_usd": 0.0,
        },
    }

    alters = {
        column_name: kwargs
        for action, value in fake_op.actions
        if action == "alter_column"
        for _table_name, column_name, kwargs in [value]
    }
    assert set(alters) == {"trace_id", "role_version", "status_changed_at", "force_closed"}
    assert all(kwargs["nullable"] is False for kwargs in alters.values())
    assert alters["trace_id"]["server_default"] is None
    assert alters["role_version"]["server_default"] is None
    assert alters["status_changed_at"]["server_default"] is None
    assert str(alters["force_closed"]["server_default"]) == "false"

    update_position = next(
        index for index, (action, _value) in enumerate(fake_op.actions) if action == "update"
    )
    index_position = next(
        index for index, (action, _value) in enumerate(fake_op.actions) if action == "create_index"
    )
    assert index_position > update_position
    assert fake_op.actions[index_position][1] == (
        "ix_agent_registry_trace_id",
        "agent_registry",
        ["trace_id"],
        False,
    )
