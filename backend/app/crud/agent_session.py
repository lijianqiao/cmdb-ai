"""CRUD operations for agent chat sessions."""

import logging
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.crud.base import CRUDBase
from app.models.agent_session import AgentSession

logger = logging.getLogger(__name__)


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
        原子抢占根会话 turn 租约；超时的陈旧租约可被接管。

        没有超时接管时，进程存活但 turn 任务已经消失的情况会让会话被**永久**锁死
        （对任何新消息返回 409），只有重启触发 recover_active_turns 才能恢复。
        `active_turn_started_at` 本来就在写，之前只是没人读它。

        接管阈值 AGENT_TURN_LEASE_TIMEOUT_SECONDS 必须大于单轮最坏耗时，
        否则会抢占一个还在正常执行的 turn，造成两个 turn 并发写同一份 transcript。

        Args:
            db: 数据库会话
            session_id: 会话主键
            token: 本次 turn 的唯一令牌

        Returns:
            抢占成功返回 True，会话有**未超时**的活跃 turn 时返回 False
        """
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=settings.AGENT_TURN_LEASE_TIMEOUT_SECONDS)

        # 先读一次旧的 started_at：接管陈旧租约是异常情况（正常 turn 会在 finally
        # 里释放），值得留日志。UPDATE ... RETURNING 拿到的是新值，所以只能预读。
        # 这不是热路径——每条用户消息一次，而同一个请求里已经做过若干次查询。
        previous_started_at = (
            await db.execute(
                select(AgentSession.active_turn_started_at).where(
                    AgentSession.id == session_id
                )
            )
        ).scalar_one_or_none()

        result = cast(
            CursorResult[tuple[()]],
            await db.execute(
                update(AgentSession)
                .where(
                    AgentSession.id == session_id,
                    or_(
                        AgentSession.active_turn_token.is_(None),
                        AgentSession.active_turn_started_at < stale_before,
                    ),
                )
                .values(active_turn_token=token, active_turn_started_at=now)
            ),
        )
        claimed = (result.rowcount or 0) == 1
        if claimed and previous_started_at is not None:
            held = previous_started_at
            if held.tzinfo is None:
                held = held.replace(tzinfo=UTC)
            logger.warning(
                "接管超时的 turn 租约 session_id=%s，原租约已持有 %.0f 秒",
                session_id,
                (now - held).total_seconds(),
            )
        return claimed

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
        result = cast(
            CursorResult[tuple[()]],
            await db.execute(
            update(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.active_turn_token == token,
            )
            .values(
                active_turn_token=None,
                active_turn_started_at=None,
            )
            ),
        )
        return (result.rowcount or 0) == 1

    async def recover_active_turns(self, db: AsyncSession) -> int:
        """
        启动时清理所有遗留的非空 turn 租约。

        Args:
            db: 数据库会话

        Returns:
            被清理的会话数量
        """
        result = cast(
            CursorResult[tuple[()]],
            await db.execute(
            update(AgentSession)
            .where(AgentSession.active_turn_token.is_not(None))
            .values(
                active_turn_token=None,
                active_turn_started_at=None,
            )
            ),
        )
        return result.rowcount or 0


agent_session_crud = CRUDAgentSession()
