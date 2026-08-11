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
