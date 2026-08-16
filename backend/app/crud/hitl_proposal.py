"""CRUD operations for HITL (human-in-the-loop) approval proposals.

State machine (docs/guide.md §5.3): PENDING -[approve]-> APPROVED -[claim]->
EXECUTING -[success]-> EXECUTED; PENDING -[reject]-> REJECTED; APPROVED
-[policy]-> REJECTED; EXECUTING -[uncertain]-> UNKNOWN; UNKNOWN -[manual]->
EXECUTED or APPROVED; EXECUTING -[never dispatched]-> APPROVED.

最后一条边（revert_unexecuted）只在执行器确认命令根本没发出去时使用：设备状态
未被改动，所以不需要 UNKNOWN 的人工核实流程，直接回到可重试的 APPROVED。
"""

import asyncio
from datetime import UTC, datetime
from typing import Literal, cast
from weakref import WeakValueDictionary

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hitl_proposal import HitlProposal

_DECISION_LOCKS: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

_UNKNOWN_REASON_CODES = frozenset({"dispatch_outcome_unknown"})
_UNEXECUTED_REASON_CODES = frozenset({"dispatch_failed_before_send"})


def _decision_lock(proposal_id: int) -> asyncio.Lock:
    """返回进程内审批锁，补足 SQLite 不支持行锁的测试与单进程场景。"""
    lock = _DECISION_LOCKS.get(proposal_id)
    if lock is None:
        lock = asyncio.Lock()
        _DECISION_LOCKS[proposal_id] = lock
    return lock


class InvalidHitlTransitionError(ValueError):
    """Raised when a HITL proposal transition violates the approval state machine."""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"cannot transition HITL proposal from {current!r} to {target!r}")


class CRUDHitlProposal:
    """HITL proposal persistence and state-machine transitions."""

    model = HitlProposal

    async def get(self, db: AsyncSession, proposal_id: int) -> HitlProposal | None:
        """Return one proposal by id."""
        stmt = select(HitlProposal).where(HitlProposal.id == proposal_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_session(
        self,
        db: AsyncSession,
        session_id: int,
        *,
        status: str | None = None,
    ) -> list[HitlProposal]:
        """按创建顺序返回会话提案，可选按状态过滤。"""
        stmt = select(HitlProposal).where(HitlProposal.session_id == session_id)
        if status is not None:
            stmt = stmt.where(HitlProposal.status == status)
        stmt = stmt.order_by(HitlProposal.id.asc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def create(
        self,
        db: AsyncSession,
        *,
        session_id: int,
        proposed_by_agent_id: str | None,
        action_type: str,
        action_payload: dict[str, object],
    ) -> HitlProposal:
        """Create a new proposal in PENDING status and flush."""
        proposal = HitlProposal(
            session_id=session_id,
            proposed_by_agent_id=proposed_by_agent_id,
            action_type=action_type,
            action_payload=action_payload,
            status="PENDING",
        )
        db.add(proposal)
        await db.flush()
        return proposal

    async def decide(
        self,
        db: AsyncSession,
        proposal_id: int,
        *,
        approve: bool,
        reviewed_by_user_id: int,
    ) -> HitlProposal:
        """Move a PENDING proposal to APPROVED or REJECTED. Only PENDING may be decided.

        使用进程内锁 + ``SELECT … FOR UPDATE`` 串行化并发审批，避免两个会话都读到
        PENDING 后互相覆盖（甚至把已 EXECUTED 的行改回 APPROVED/REJECTED）。
        """
        target = "APPROVED" if approve else "REJECTED"
        async with _decision_lock(proposal_id):
            stmt = (
                select(HitlProposal)
                .where(HitlProposal.id == proposal_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            result = await db.execute(stmt)
            proposal = result.scalar_one_or_none()
            if proposal is None:
                raise ValueError(f"HITL proposal {proposal_id} not found")
            if proposal.status != "PENDING":
                raise InvalidHitlTransitionError(proposal.status, target)

            proposal.status = target
            proposal.reviewed_by_user_id = reviewed_by_user_id
            proposal.reviewed_at = datetime.now(UTC)
            await db.flush()
            return proposal

    async def claim_execution(self, db: AsyncSession, proposal_id: int) -> HitlProposal:
        """原子认领 APPROVED 提案，转为 EXECUTING 并记录 execution_started_at。"""
        now = datetime.now(UTC)
        stmt = (
            update(HitlProposal)
            .where(HitlProposal.id == proposal_id, HitlProposal.status == "APPROVED")
            .values(
                status="EXECUTING",
                execution_started_at=now,
                status_reason=None,
            )
            .returning(HitlProposal)
        )
        claimed = (await db.execute(stmt)).scalar_one_or_none()
        if claimed is None:
            current = await self.get(db, proposal_id)
            if current is None:
                raise ValueError(f"HITL proposal {proposal_id} not found")
            raise InvalidHitlTransitionError(current.status, "EXECUTING")
        await db.flush()
        return claimed

    async def revert_unexecuted(
        self,
        db: AsyncSession,
        proposal_id: int,
        *,
        reason: str,
    ) -> HitlProposal:
        """确定命令未下发时把 EXECUTING 回退成 APPROVED，原因仅允许固定安全代码。

        只有执行器明确报告"没碰到设备"（连接都没建起来）才允许走这条边：此时
        设备状态没被改动，回到 APPROVED 让管理员修好前置条件后直接重试即可，
        不必像 UNKNOWN 那样先人工核实设备实际状态。
        """
        if reason not in _UNEXECUTED_REASON_CODES:
            raise ValueError(f"unsupported HITL unexecuted reason code: {reason!r}")

        stmt = (
            update(HitlProposal)
            .where(HitlProposal.id == proposal_id, HitlProposal.status == "EXECUTING")
            .values(
                status="APPROVED",
                status_reason=reason,
                execution_started_at=None,
            )
            .returning(HitlProposal)
        )
        reverted = (await db.execute(stmt)).scalar_one_or_none()
        if reverted is None:
            current = await self.get(db, proposal_id)
            if current is None:
                raise ValueError(f"HITL proposal {proposal_id} not found")
            raise InvalidHitlTransitionError(current.status, "APPROVED")
        await db.flush()
        return reverted

    async def reject_for_policy(self, db: AsyncSession, proposal_id: int) -> HitlProposal:
        """策略复检拒绝：将 APPROVED 提案转为 REJECTED(policy_blacklisted)。"""
        stmt = (
            update(HitlProposal)
            .where(HitlProposal.id == proposal_id, HitlProposal.status == "APPROVED")
            .values(
                status="REJECTED",
                status_reason="policy_blacklisted",
            )
            .returning(HitlProposal)
        )
        rejected = (await db.execute(stmt)).scalar_one_or_none()
        if rejected is None:
            current = await self.get(db, proposal_id)
            if current is None:
                raise ValueError(f"HITL proposal {proposal_id} not found")
            raise InvalidHitlTransitionError(current.status, "REJECTED")
        await db.flush()
        return rejected

    async def mark_unknown(
        self,
        db: AsyncSession,
        proposal_id: int,
        *,
        reason: str,
    ) -> HitlProposal:
        """将 EXECUTING 提案标记为 UNKNOWN，原因仅允许固定安全代码。"""
        if reason not in _UNKNOWN_REASON_CODES:
            raise ValueError(f"unsupported HITL unknown reason code: {reason!r}")

        stmt = (
            update(HitlProposal)
            .where(HitlProposal.id == proposal_id, HitlProposal.status == "EXECUTING")
            .values(
                status="UNKNOWN",
                status_reason=reason,
            )
            .returning(HitlProposal)
        )
        unknown = (await db.execute(stmt)).scalar_one_or_none()
        if unknown is None:
            current = await self.get(db, proposal_id)
            if current is None:
                raise ValueError(f"HITL proposal {proposal_id} not found")
            raise InvalidHitlTransitionError(current.status, "UNKNOWN")
        await db.flush()
        return unknown

    async def resolve_unknown(
        self,
        db: AsyncSession,
        proposal_id: int,
        *,
        resolution: Literal["confirm_executed", "allow_retry"],
        resolved_by_user_id: int,
    ) -> HitlProposal:
        """人工处置 UNKNOWN：确认已执行或允许重试。"""
        now = datetime.now(UTC)
        if resolution == "confirm_executed":
            stmt = (
                update(HitlProposal)
                .where(HitlProposal.id == proposal_id, HitlProposal.status == "UNKNOWN")
                .values(
                    status="EXECUTED",
                    status_reason="manual_confirmed",
                    executed_at=now,
                    resolved_by_user_id=resolved_by_user_id,
                    resolved_at=now,
                )
                .returning(HitlProposal)
            )
            target = "EXECUTED"
        else:
            stmt = (
                update(HitlProposal)
                .where(HitlProposal.id == proposal_id, HitlProposal.status == "UNKNOWN")
                .values(
                    status="APPROVED",
                    status_reason="retry_authorized",
                    execution_started_at=None,
                    resolved_by_user_id=resolved_by_user_id,
                    resolved_at=now,
                )
                .returning(HitlProposal)
            )
            target = "APPROVED"

        resolved = (await db.execute(stmt)).scalar_one_or_none()
        if resolved is None:
            current = await self.get(db, proposal_id)
            if current is None:
                raise ValueError(f"HITL proposal {proposal_id} not found")
            raise InvalidHitlTransitionError(current.status, target)
        await db.flush()
        return resolved

    async def recover_executing(self, db: AsyncSession) -> int:
        """启动恢复：将所有 EXECUTING 提案转为 UNKNOWN，禁止崩溃后自动重试。"""
        stmt = (
            update(HitlProposal)
            .where(HitlProposal.status == "EXECUTING")
            .values(
                status="UNKNOWN",
                status_reason="dispatch_outcome_unknown",
            )
        )
        result = cast(
            CursorResult[tuple[()]],
            await db.execute(stmt),
        )
        await db.flush()
        return result.rowcount or 0

    async def mark_executed(self, db: AsyncSession, proposal_id: int) -> HitlProposal:
        """将 EXECUTING 提案转为 EXECUTED，仅允许从 EXECUTING 进入。"""
        now = datetime.now(UTC)
        stmt = (
            update(HitlProposal)
            .where(HitlProposal.id == proposal_id, HitlProposal.status == "EXECUTING")
            .values(
                status="EXECUTED",
                status_reason="executor_succeeded",
                executed_at=now,
            )
            .returning(HitlProposal)
        )
        executed = (await db.execute(stmt)).scalar_one_or_none()
        if executed is None:
            current = await self.get(db, proposal_id)
            if current is None:
                raise ValueError(f"HITL proposal {proposal_id} not found")
            raise InvalidHitlTransitionError(current.status, "EXECUTED")
        await db.flush()
        return executed

    async def list_snapshot_for_session(
        self, db: AsyncSession, session_id: int
    ) -> list[HitlProposal]:
        """返回会话中可恢复态提案与已执行的设备查询。"""
        stmt = (
            select(HitlProposal)
            .where(
                HitlProposal.session_id == session_id,
                or_(
                    HitlProposal.status.in_(
                        ("PENDING", "APPROVED", "EXECUTING", "UNKNOWN")
                    ),
                    and_(
                        HitlProposal.status == "EXECUTED",
                        HitlProposal.action_type == "device_query",
                    ),
                ),
            )
            .order_by(HitlProposal.id.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


hitl_proposal_crud = CRUDHitlProposal()
