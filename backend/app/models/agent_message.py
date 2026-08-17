"""Append-only transcript of one agent session.

`tool_calls` carries the raw tool-call requests attached to an *assistant* row
(list of ``{"id", "name", "arguments"}`` dicts) so the exact request can be
replayed into the next model call. `tool_call_id` is used the other direction —
on a *tool* row, it names which call this row is the result of. A row never
uses both.
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentMessage(Base):
    """One root- or child-Agent message in a shared user session."""

    __tablename__ = "agent_messages"
    __table_args__ = (
        # 末尾带上 id：三列同时覆盖 list_for_agent / list_for_agent_after_id /
        # list_root_before_id 的 WHERE 与 ORDER BY id，让分页走纯索引扫描。
        # 不写 DESC——排序只有 id 一列，反向扫升序索引即可。
        Index("ix_agent_messages_session_agent_id", "session_id", "agent_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_calls: Mapped[list[dict[str, str]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"<AgentMessage(id={self.id}, session_id={self.session_id}, role={self.role!r})>"
