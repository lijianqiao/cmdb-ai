"""HITL (human-in-the-loop) approval proposal for sensitive agent actions.

`asset_id` for device-oriented proposals lives inside `action_payload` (JSON),
not as a dedicated foreign key — this keeps this table independent of the
CMDB subsystem (see docs/AGENT_ARCHITECTURE.md assumption A7).
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class HitlProposal(Base):
    """One write-action proposal awaiting human approval."""

    __tablename__ = "hitl_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposed_by_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_registry.child_id", ondelete="SET NULL"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    action_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"<HitlProposal(id={self.id}, action_type={self.action_type!r}, status={self.status!r})>"
