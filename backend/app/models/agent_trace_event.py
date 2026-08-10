"""Append-only observability trace events for the agent runtime.

`agent_id`/`parent_agent_id` are plain strings, not foreign keys to
`agent_registry.child_id` — the root agent (which is not a spawned child and
has no registry row) also emits trace events under its own synthetic id.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentTraceEvent(Base):
    """One observability span emitted by the agent loop, a tool call, or spawn lifecycle."""

    __tablename__ = "agent_trace_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    span_type: Mapped[str] = mapped_column(String(20), nullable=False)
    tool: Mapped[str | None] = mapped_column(String(100), nullable=True)
    control: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"<AgentTraceEvent(id={self.id}, span_type={self.span_type!r}, tool={self.tool!r})>"
