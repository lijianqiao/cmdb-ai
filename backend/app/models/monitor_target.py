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
