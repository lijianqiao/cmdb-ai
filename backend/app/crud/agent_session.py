"""CRUD operations for agent chat sessions.

Sessions are archived, never destroyed by their owners: archiving hides the chat
from the owner while its HITL proposals and execution results stay as evidence
(see ``archive``). Archived sessions are excluded from listing and cannot start
new turns.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.crud.base import CRUDBase
from app.models.agent_registry import AgentRegistry
from app.models.agent_session import AgentSession
from app.models.hitl_proposal import HitlProposal

logger = logging.getLogger(__name__)

type ArchiveOutcome = Literal[
    "archived",
    "not_found",
    "active_turn",
    "active_children",
    "unsettled_proposals",
]

# 这几种状态的提案还需要人盯着：待执行、执行中、结果不确定。聊天一收起就没人能继续核实了。
_UNSETTLED_PROPOSAL_STATUSES = ("APPROVED", "EXECUTING", "UNKNOWN")
# 子 Agent 仍在运行的状态（与 spawn.types._ACTIVE_STATUSES 一致）
_ACTIVE_CHILD_STATUSES = ("REQUESTED", "SPAWNING", "RUNNING")
# 归档时撤回的待审批提案记下的状态原因
WITHDRAWN_ON_ARCHIVE = "withdrawn_on_archive"


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """归档结果；成功时附带随归档撤回的待审批提案，供调用方逐条写审计。"""

    outcome: ArchiveOutcome
    withdrawn_proposal_ids: tuple[int, ...] = ()


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
        """Return one user's active (non-archived) sessions newest-first with a total count."""
        count_stmt = select(func.count()).select_from(AgentSession).where(
            AgentSession.user_id == user_id,
            AgentSession.status == "active",
        )
        total = (await db.execute(count_stmt)).scalar_one()

        stmt = (
            select(AgentSession)
            .where(AgentSession.user_id == user_id, AgentSession.status == "active")
            .order_by(AgentSession.id.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all()), total

    async def hard_delete(self, db: AsyncSession, session_id: int) -> bool:
        """
        物理删除会话，只给系统内部的临时会话用（如知识库分类作业）；用户删聊天走 archive。

        消息/registry/trace 依赖库级 CASCADE 一起删；HITL 提案的外键是 RESTRICT，
        会话一旦产生过提案就删不掉（抛 IntegrityError），证据不会被连带销毁。

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

    async def archive(self, db: AsyncSession, session_id: int) -> ArchiveResult:
        """
        归档会话：从所有者视野里收起，提案与执行证据原样保留；还有事情在跑时拒绝。

        还没审批的提案随归档撤回（REJECTED + withdrawn_on_archive，审批人记为会话所有者）：
        聊天看不见之后，提案若还能被批准就会到设备上执行、结果写进一个隐藏的聊天；
        也不能因此拒绝归档——没有审批权限的用户拒绝不了自己的提案。
        归档成功后会话里只剩已结束（EXECUTED / REJECTED）的提案。

        锁顺序：先锁会话行，再锁该会话未结束的提案行，然后才检查、更新。
        claim_turn 更新的是同一会话行、审批要锁同一提案行，所以不会出现「刚检查完没有
        turn / 待执行提案，紧接着又起了一轮或刚被批准」的竞态。锁只持有到本事务提交，
        不跨任何设备网络调用。

        Args:
            db: 数据库会话（调用方负责提交并据 withdrawn_proposal_ids 写审计）
            session_id: 会话主键

        Returns:
            outcome 为 archived 表示已归档；not_found 表示不存在或已归档；其余为拒绝原因
        """
        session = (
            await db.execute(
                select(AgentSession)
                .where(AgentSession.id == session_id, AgentSession.status == "active")
                .with_for_update()
            )
        ).scalar_one_or_none()
        if session is None:
            return ArchiveResult("not_found")

        if session.active_turn_token is not None and session.active_turn_started_at is not None:
            started = session.active_turn_started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            stale_before = datetime.now(UTC) - timedelta(
                seconds=settings.AGENT_TURN_LEASE_TIMEOUT_SECONDS
            )
            if started >= stale_before:
                return ArchiveResult("active_turn")

        active_children = await db.scalar(
            select(func.count())
            .select_from(AgentRegistry)
            .where(
                AgentRegistry.session_id == session_id,
                AgentRegistry.status.in_(_ACTIVE_CHILD_STATUSES),
            )
        )
        if active_children:
            return ArchiveResult("active_children")

        # PostgreSQL 的 FOR UPDATE 不能和 count() 一起用，所以取行回来在 Python 里判断
        open_proposals = (
            await db.execute(
                select(HitlProposal)
                .where(
                    HitlProposal.session_id == session_id,
                    HitlProposal.status.in_(("PENDING", *_UNSETTLED_PROPOSAL_STATUSES)),
                )
                .order_by(HitlProposal.id)
                .with_for_update()
            )
        ).scalars().all()
        if any(proposal.status != "PENDING" for proposal in open_proposals):
            return ArchiveResult("unsettled_proposals")

        now = datetime.now(UTC)
        for proposal in open_proposals:
            proposal.status = "REJECTED"
            proposal.status_reason = WITHDRAWN_ON_ARCHIVE
            proposal.reviewed_by_user_id = session.user_id
            proposal.reviewed_at = now
        session.status = "archived"
        await db.flush()
        return ArchiveResult(
            "archived", tuple(proposal.id for proposal in open_proposals)
        )

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
                    # 归档的会话不能再起新的一轮，否则归档之后还能长出新提案
                    AgentSession.status == "active",
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
