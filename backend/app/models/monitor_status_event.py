"""Monitor status event — probe result log with same-status upsert.

A target's "current" online/offline status is never stored separately; it is
always derived from the latest row here (see app/crud/monitor_status_event.py
`get_latest_status_for_targets`), per docs/AGENT_ARCHITECTURE.md §3's rule
against maintaining two sources of truth for the same fact. Repeated probes
with the same status update the current row; only a status flip appends a new
row.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MonitorStatusEvent(Base):
    """One probe result for one target."""

    __tablename__ = "monitor_status_events"
    __table_args__ = (
        # 支撑 get_latest_status_for_targets / list_recent_for_targets / purge_older_than 的
        # PARTITION BY target_id ORDER BY checked_at DESC, id DESC，
        # 让 PostgreSQL 反向扫描索引直接产出所需顺序，省掉 WindowAgg 上游的全量 Sort。
        # 两个排序列方向一致，所以升序索引即可，不需要声明 DESC。
        Index("ix_monitor_status_events_target_checked", "target_id", "checked_at", "id"),
    )

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
