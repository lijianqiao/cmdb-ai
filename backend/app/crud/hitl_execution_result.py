"""CRUD operations for persisted HITL device-query execution results."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hitl_execution_result import HitlExecutionResult


class CRUDHitlExecutionResult:
    """Store and locate the unique full result belonging to a proposal."""

    async def get_by_proposal(
        self, db: AsyncSession, proposal_id: int
    ) -> HitlExecutionResult | None:
        """Return the full result for one proposal, if it exists."""
        stmt = select(HitlExecutionResult).where(HitlExecutionResult.proposal_id == proposal_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def create_for_proposal(
        self,
        db: AsyncSession,
        *,
        proposal_id: int,
        content: str,
    ) -> HitlExecutionResult:
        """Create a pending result once; repeat calls return the stored row."""
        existing = await self.get_by_proposal(db, proposal_id)
        if existing is not None:
            return existing
        row = HitlExecutionResult(
            proposal_id=proposal_id,
            content=content,
            content_length=len(content),
            summary_status="pending",
        )
        db.add(row)
        await db.flush()
        return row

    async def existing_proposal_ids(
        self, db: AsyncSession, proposal_ids: list[int]
    ) -> set[int]:
        """Return result-bearing proposal IDs without selecting result content."""
        if not proposal_ids:
            return set()
        stmt = select(HitlExecutionResult.proposal_id).where(
            HitlExecutionResult.proposal_id.in_(proposal_ids)
        )
        result = await db.execute(stmt)
        return set(result.scalars().all())


hitl_execution_result_crud = CRUDHitlExecutionResult()
