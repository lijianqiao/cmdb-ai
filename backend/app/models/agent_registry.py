"""Child-agent registry — the durable ChildReceipt store for dynamic spawn.

`child_id` is a string (UUID4), not an autoincrement int, mirroring the
existing `RefreshSessionFamily.id` string-primary-key precedent — child agents
are referenced across process/session boundaries and need a stable opaque id.
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentRegistry(Base):
    """One spawned child agent instance and its ChildReceipt."""

    __tablename__ = "agent_registry"

    child_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_agent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_registry.child_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    agent_path: Mapped[str] = mapped_column(String(500), nullable=False)
    trace_id: Mapped[str] = mapped_column(
        String(36), nullable=False, default=lambda: str(uuid4()), index=True
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    role_version: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    tools_allowlist: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    sandbox_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="read-only")
    task_brief: Mapped[str] = mapped_column(Text, nullable=False)
    budget: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="REQUESTED", index=True)
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    force_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifacts: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<AgentRegistry(child_id={self.child_id!r}, role={self.role!r}, status={self.status!r})>"
