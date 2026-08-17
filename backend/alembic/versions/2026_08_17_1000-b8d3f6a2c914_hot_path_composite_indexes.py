"""Add composite indexes for the monitor and agent-transcript hot paths.

Revision ID: b8d3f6a2c914
Revises: a7c9e2f4b681
Create Date: 2026-08-17 10:00:00+00:00

两个索引都用升序列，不写 DESC：目标查询的排序列方向一致
（``ORDER BY checked_at DESC, id DESC`` / ``ORDER BY id DESC``），
PostgreSQL 反向扫描升序 B-tree 即可满足，DESC 索引只在混合方向时才必要。
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "b8d3f6a2c914"
down_revision: str | None = "a7c9e2f4b681"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_destructive_downgrade() -> None:
    """Require an explicit opt-in before removing production indexes."""
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


# (索引名, 表名, 列)
INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    # 支撑 get_latest_status_for_targets / list_recent_for_targets / purge_older_than 的
    # PARTITION BY target_id ORDER BY checked_at DESC, id DESC，消除 WindowAgg 上游的 Sort。
    (
        "ix_monitor_status_events_target_checked",
        "monitor_status_events",
        ["target_id", "checked_at", "id"],
    ),
    # 支撑 list_for_agent / list_for_agent_after_id / list_root_before_id 的
    # WHERE session_id=? AND agent_id IS/= ? ORDER BY id，让分页走纯索引扫描。
    (
        "ix_agent_messages_session_agent_id",
        "agent_messages",
        ["session_id", "agent_id", "id"],
    ),
)


def _is_postgresql() -> bool:
    return op.get_context().dialect.name == "postgresql"


# 被新复合索引完全覆盖的旧索引：(session_id, agent_id) 是
# (session_id, agent_id, id) 的严格最左前缀，任何它能服务的查询新索引都能服务，
# 因此可以在同一个 revision 里安全移除，不存在"没有索引可用"的窗口。
SUPERSEDED_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_agent_messages_session_id_agent_id", "agent_messages"),
)


def upgrade() -> None:
    """Create rerunnable concurrent composite indexes in an index-only revision."""
    if not _is_postgresql():
        return

    with op.get_context().autocommit_block():
        for name, table, columns in INDEXES:
            # 并发建索引失败会留下 INVALID 索引；先按受管名 drop 让本 revision 可重跑。
            op.drop_index(name, table_name=table, if_exists=True, postgresql_concurrently=True)
            op.create_index(name, table, columns, postgresql_concurrently=True)
        # 新索引建好之后再删旧索引，保证全程都有可用索引。
        for name, table in SUPERSEDED_INDEXES:
            op.drop_index(name, table_name=table, if_exists=True, postgresql_concurrently=True)


def downgrade() -> None:
    """Restore the superseded index, then drop the composite indexes."""
    _require_destructive_downgrade()
    if not _is_postgresql():
        return

    with op.get_context().autocommit_block():
        op.create_index(
            "ix_agent_messages_session_id_agent_id",
            "agent_messages",
            ["session_id", "agent_id"],
            postgresql_concurrently=True,
        )
        for name, table, _columns in reversed(INDEXES):
            op.drop_index(name, table_name=table, if_exists=True, postgresql_concurrently=True)
