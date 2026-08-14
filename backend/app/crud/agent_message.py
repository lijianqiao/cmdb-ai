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
        agent_id: str | None = None,
        tool_call_id: str | None = None,
        tool_calls: list[dict[str, str]] | None = None,
    ) -> AgentMessage:
        """Append one root- or child-scoped message and flush."""
        message = AgentMessage(
            session_id=session_id,
            agent_id=agent_id,
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
        if limit is not None:
            return messages[-limit:] if limit else []
        return messages

    async def list_for_agent(
        self,
        db: AsyncSession,
        session_id: int,
        *,
        agent_id: str | None,
        limit: int | None = None,
    ) -> list[AgentMessage]:
        """Return only root (`None`) or one exact child's messages, oldest-first."""
        agent_filter = (
            AgentMessage.agent_id.is_(None)
            if agent_id is None
            else AgentMessage.agent_id == agent_id
        )
        stmt = select(AgentMessage).where(
            AgentMessage.session_id == session_id, agent_filter
        )
        if limit is not None:
            if limit <= 0:
                return []
            stmt = stmt.order_by(AgentMessage.id.desc()).limit(limit)
        else:
            stmt = stmt.order_by(AgentMessage.id.asc())
        result = await db.execute(stmt)
        messages = list(result.scalars().all())
        if limit is not None:
            messages.reverse()
        return messages

    async def list_for_agent_after_id(
        self,
        db: AsyncSession,
        session_id: int,
        *,
        agent_id: str | None,
        after_id: int | None,
        limit: int,
    ) -> list[AgentMessage]:
        """Return messages with id > after_id, oldest-first, capped to limit."""
        if limit <= 0:
            return []
        agent_filter = (
            AgentMessage.agent_id.is_(None)
            if agent_id is None
            else AgentMessage.agent_id == agent_id
        )
        stmt = select(AgentMessage).where(
            AgentMessage.session_id == session_id,
            agent_filter,
        )
        if after_id is not None:
            stmt = stmt.where(AgentMessage.id > after_id)
        stmt = stmt.order_by(AgentMessage.id.desc()).limit(limit)
        result = await db.execute(stmt)
        messages = list(result.scalars().all())
        messages.reverse()
        return messages

    async def list_root_before_id(
        self,
        db: AsyncSession,
        session_id: int,
        *,
        before_id: int | None,
        limit: int,
    ) -> tuple[list[AgentMessage], bool]:
        """按 cursor 分页返回根消息，页内按 id 升序（最旧在前）。"""
        stmt = select(AgentMessage).where(
            AgentMessage.session_id == session_id,
            AgentMessage.agent_id.is_(None),
        )
        if before_id is not None:
            stmt = stmt.where(AgentMessage.id < before_id)
        rows = list(
            (await db.execute(stmt.order_by(AgentMessage.id.desc()).limit(limit + 1)))
            .scalars()
            .all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        rows.reverse()
        return rows, has_more


agent_message_crud = CRUDAgentMessage()
