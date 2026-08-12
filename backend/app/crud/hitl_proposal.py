"""CRUD operations for HITL (human-in-the-loop) approval proposals.

State machine (docs/guide.md §5.3): PENDING -[approve]-> APPROVED -[resume]->
EXECUTED (exactly once); PENDING -[reject]-> REJECTED. Only PENDING may be
decided; only APPROVED may become EXECUTED.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hitl_proposal import HitlProposal


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
        """Move a PENDING proposal to APPROVED or REJECTED. Only PENDING may be decided."""
        proposal = await self.get(db, proposal_id)
        if proposal is None:
            raise ValueError(f"HITL proposal {proposal_id} not found")
        target = "APPROVED" if approve else "REJECTED"
        if proposal.status != "PENDING":
            raise InvalidHitlTransitionError(proposal.status, target)

        proposal.status = target
        proposal.reviewed_by_user_id = reviewed_by_user_id
        proposal.reviewed_at = datetime.now(UTC)
        await db.flush()
        return proposal

    async def mark_executed(self, db: AsyncSession, proposal_id: int) -> HitlProposal:
        """Move an APPROVED proposal to EXECUTED exactly once."""
        proposal = await self.get(db, proposal_id)
        if proposal is None:
            raise ValueError(f"HITL proposal {proposal_id} not found")
        if proposal.status != "APPROVED":
            raise InvalidHitlTransitionError(proposal.status, "EXECUTED")

        proposal.status = "EXECUTED"
        proposal.executed_at = datetime.now(UTC)
        await db.flush()
        return proposal


hitl_proposal_crud = CRUDHitlProposal()
