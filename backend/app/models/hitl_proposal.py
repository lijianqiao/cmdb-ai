"""HITL (human-in-the-loop) approval proposal for sensitive agent actions.

`asset_id` for device-oriented proposals lives inside `action_payload` (JSON),
not as a dedicated foreign key — this keeps this table independent of the
CMDB subsystem (see docs/AGENT_ARCHITECTURE.md assumption A7).

Proposals are operational evidence and outlive the chat that produced them:
``session_id`` is ON DELETE RESTRICT so neither deleting a session nor purging
its owner can cascade them away (chats are archived instead). The evidence
columns freeze who asked, how it was approved and what the target looked like
at proposal time; legacy rows leave them NULL rather than faking history.
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
        Integer,
        ForeignKey(
            "agent_sessions.id",
            ondelete="RESTRICT",
            name="fk_hitl_proposals_session_id_agent_sessions",
        ),
        nullable=False,
        index=True,
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
    execution_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resolved_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 申请人：提出这条提案的会话所有者
    requested_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_hitl_proposals_requested_by_user_id_users",
        ),
        nullable=True,
    )
    # 审批方式：manual（人工）/ auto:<档位>（会话档位自动批准）
    approval_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 提案当时的资产与命令快照；不含密码
    evidence_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"<HitlProposal(id={self.id}, action_type={self.action_type!r}, status={self.status!r})>"
