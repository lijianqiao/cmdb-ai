"""CRUD operations for the child-agent registry (ChildReceipt store).

Implements the lifecycle state machine from docs/guide.md §7.3:
REQUESTED -> SPAWNING -> RUNNING -> COMPLETED|FAILED|CANCELLED -> CLOSED.
`close()` is the one operation allowed from any non-terminal status — it is
the forced-detach escape valve so a hung child can always free its slot.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_registry import AgentRegistry

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "REQUESTED": {"SPAWNING", "FAILED", "CANCELLED"},
    "SPAWNING": {"RUNNING", "FAILED", "CANCELLED"},
    "RUNNING": {"COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": {"CLOSED"},
    "FAILED": {"CLOSED"},
    "CANCELLED": {"CLOSED"},
    "CLOSED": set(),
}


class InvalidAgentStatusTransitionError(ValueError):
    """Raised when a status transition violates the agent lifecycle state machine."""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"cannot transition agent status from {current!r} to {target!r}")


class CRUDAgentRegistry:
    """Child-agent registry persistence and lifecycle transitions."""

    model = AgentRegistry

    async def get(self, db: AsyncSession, child_id: str) -> AgentRegistry | None:
        """Return one registry row by child_id."""
        stmt = select(AgentRegistry).where(AgentRegistry.child_id == child_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        db: AsyncSession,
        *,
        session_id: int,
        parent_agent_id: str | None,
        agent_path: str,
        role: str,
        model: str,
        tools_allowlist: list[str],
        sandbox_mode: str,
        task_brief: str,
        budget: dict[str, object],
    ) -> AgentRegistry:
        """Register a newly spawned child agent in REQUESTED status and flush."""
        registry = AgentRegistry(
            session_id=session_id,
            parent_agent_id=parent_agent_id,
            agent_path=agent_path,
            role=role,
            model=model,
            tools_allowlist=tools_allowlist,
            sandbox_mode=sandbox_mode,
            task_brief=task_brief,
            budget=budget,
            status="REQUESTED",
        )
        db.add(registry)
        await db.flush()
        return registry

    async def transition_status(
        self,
        db: AsyncSession,
        child_id: str,
        target_status: str,
        *,
        result_summary: str | None = None,
        artifacts: list[str] | None = None,
    ) -> AgentRegistry:
        """Move a child agent to `target_status`, enforcing the lifecycle state machine."""
        registry = await self.get(db, child_id)
        if registry is None:
            raise ValueError(f"agent registry {child_id!r} not found")

        allowed = _ALLOWED_TRANSITIONS.get(registry.status, set())
        if target_status not in allowed:
            raise InvalidAgentStatusTransitionError(registry.status, target_status)

        registry.status = target_status
        if result_summary is not None:
            registry.result_summary = result_summary
        if artifacts is not None:
            registry.artifacts = artifacts
        if target_status == "CLOSED":
            registry.closed_at = datetime.now(UTC)

        await db.flush()
        return registry

    async def close(self, db: AsyncSession, child_id: str) -> AgentRegistry:
        """Idempotently close a child agent, bypassing the normal transition table."""
        registry = await self.get(db, child_id)
        if registry is None:
            raise ValueError(f"agent registry {child_id!r} not found")
        if registry.status != "CLOSED":
            registry.status = "CLOSED"
            registry.closed_at = datetime.now(UTC)
            await db.flush()
        return registry

    async def list_active_children(self, db: AsyncSession, session_id: int) -> list[AgentRegistry]:
        """Return every child in this session not yet CLOSED, oldest-first."""
        stmt = (
            select(AgentRegistry)
            .where(AgentRegistry.session_id == session_id, AgentRegistry.status != "CLOSED")
            .order_by(AgentRegistry.created_at.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


agent_registry_crud = CRUDAgentRegistry()
