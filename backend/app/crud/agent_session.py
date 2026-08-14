"""CRUD operations for agent chat sessions."""

from datetime import UTC, datetime

from sqlalchemy import func, select, update
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

    async def claim_turn(self, db: AsyncSession, session_id: int, token: str) -> bool:
        """
        原子抢占根会话 turn 租约。

        Args:
            db: 数据库会话
            session_id: 会话主键
            token: 本次 turn 的唯一令牌

        Returns:
            抢占成功返回 True，会话已有活跃 turn 时返回 False
        """
        result = await db.execute(
            update(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.active_turn_token.is_(None),
            )
            .values(
                active_turn_token=token,
                active_turn_started_at=datetime.now(UTC),
            )
        )
        return result.rowcount == 1

    async def release_turn(self, db: AsyncSession, session_id: int, token: str) -> bool:
        """
        释放 turn 租约（仅持有者可释放）。

        Args:
            db: 数据库会话
            session_id: 会话主键
            token: 抢占时写入的令牌

        Returns:
            释放成功返回 True，令牌不匹配或已无租约时返回 False
        """
        result = await db.execute(
            update(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.active_turn_token == token,
            )
            .values(
                active_turn_token=None,
                active_turn_started_at=None,
            )
        )
        return result.rowcount == 1

    async def recover_active_turns(self, db: AsyncSession) -> int:
        """
        启动时清理所有遗留的非空 turn 租约。

        Args:
            db: 数据库会话

        Returns:
            被清理的会话数量
        """
        result = await db.execute(
            update(AgentSession)
            .where(AgentSession.active_turn_token.is_not(None))
            .values(
                active_turn_token=None,
                active_turn_started_at=None,
            )
        )
        return result.rowcount


agent_session_crud = CRUDAgentSession()
