"""CRUD operations for agent chat sessions."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.agent_session import AgentSession


class CRUDAgentSession(CRUDBase[AgentSession]):
    """Agent session persistence; generic get/create/update come from CRUDBase."""

    model = AgentSession

    async def list_for_user(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[AgentSession], int]:
        """Return one user's sessions newest-first with a total count."""
        count_stmt = select(func.count()).select_from(AgentSession).where(
            AgentSession.user_id == user_id
        )
        total = (await db.execute(count_stmt)).scalar_one()

        stmt = (
            select(AgentSession)
            .where(AgentSession.user_id == user_id)
            .order_by(AgentSession.id.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all()), total

    async def hard_delete(self, db: AsyncSession, session_id: int) -> bool:
        """
        物理删除会话（关联消息/HITL/registry/trace 依赖库级 CASCADE）。

        Args:
            db: 数据库会话
            session_id: 会话主键

        Returns:
            找到并删除返回 True，否则 False
        """
        session = await self.get(db, session_id)
        if session is None:
            return False
        await db.delete(session)
        await db.flush()
        return True


agent_session_crud = CRUDAgentSession()
