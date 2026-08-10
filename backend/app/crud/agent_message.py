"""CRUD operations for agent transcript messages.

This is intentionally not a `CRUDBase` subclass: messages are append-only (no
update, no soft-delete), so the generic base's machinery does not apply.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_message import AgentMessage


class CRUDAgentMessage:
    """Append-only transcript storage for one agent session."""

    model = AgentMessage

    async def append(
        self,
        db: AsyncSession,
        *,
        session_id: int,
        role: str,
        content: str,
        tool_call_id: str | None = None,
        tool_calls: list[dict[str, str]] | None = None,
    ) -> AgentMessage:
        """Append one message to a session's transcript and flush."""
        message = AgentMessage(
            session_id=session_id,
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            tool_calls=tool_calls,
        )
        db.add(message)
        await db.flush()
        return message

    async def list_for_session(
        self,
        db: AsyncSession,
        session_id: int,
        *,
        limit: int | None = None,
    ) -> list[AgentMessage]:
        """Return a session's messages oldest-first, optionally capped to the most recent `limit`."""
        stmt = (
            select(AgentMessage)
            .where(AgentMessage.session_id == session_id)
            .order_by(AgentMessage.id.asc())
        )
        result = await db.execute(stmt)
        messages = list(result.scalars().all())
        if limit is not None and len(messages) > limit:
            return messages[-limit:]
        return messages


agent_message_crud = CRUDAgentMessage()
