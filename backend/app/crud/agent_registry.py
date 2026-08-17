"""CRUD operations for the child-agent registry (ChildReceipt store).

Implements the lifecycle state machine from docs/guide.md §7.3:
REQUESTED -> SPAWNING -> RUNNING -> COMPLETED|FAILED|CANCELLED -> CLOSED.
`close()` is the one operation allowed from any non-terminal status — it is
the forced-detach escape valve so a hung child can always free its slot.
"""

from datetime import UTC, datetime
from math import fsum, isfinite

from sqlalchemy import func, select
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

_ACTIVE_STATUSES = {"REQUESTED", "SPAWNING", "RUNNING"}
_TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
_ACTUAL_COST_STATUSES = _TERMINAL_STATUSES | {"CLOSED"}
_BUDGET_DEFAULTS: dict[str, int | float] = {
    "max_steps": 20,
    "max_cost_usd": 1.0,
    "max_wall_time_seconds": 120.0,
    "steps_used": 0,
    "cost_used_usd": 0.0,
}


def _normalize_budget(budget: dict[str, object]) -> dict[str, object]:
    """Return the fixed five-key receipt budget while preserving supplied values."""
    return {key: budget.get(key, default) for key, default in _BUDGET_DEFAULTS.items()}


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
        trace_id: str,
        role_version: str,
        parent_agent_id: str | None,
        agent_path: str,
        role: str,
        model: str,
        tools_allowlist: list[str],
        sandbox_mode: str,
        task_brief: str,
        budget: dict[str, object],
        child_id: str | None = None,
    ) -> AgentRegistry:
        """Register a newly spawned child agent in REQUESTED status and flush."""
        registry = AgentRegistry(
            session_id=session_id,
            parent_agent_id=parent_agent_id,
            agent_path=agent_path,
            trace_id=trace_id,
            role=role,
            role_version=role_version,
            model=model,
            tools_allowlist=tools_allowlist,
            sandbox_mode=sandbox_mode,
            task_brief=task_brief,
            budget=_normalize_budget(budget),
            status="REQUESTED",
        )
        if child_id is not None:
            registry.child_id = child_id
        db.add(registry)
        await db.flush()
        return registry

    async def transition_status(
        self,
        db: AsyncSession,
        child_id: str,
        target_status: str,
        *,
        budget: dict[str, object] | None = None,
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

        changed_at = datetime.now(UTC)
        registry.status = target_status
        registry.status_changed_at = changed_at
        if budget is not None:
            registry.budget = _normalize_budget({**registry.budget, **budget})
        if result_summary is not None:
            registry.result_summary = result_summary
        if artifacts is not None:
            registry.artifacts = artifacts
        if target_status == "CLOSED":
            registry.closed_at = changed_at

        await db.flush()
        return registry

    async def close(
        self, db: AsyncSession, child_id: str, *, force_closed: bool = False
    ) -> AgentRegistry:
        """Idempotently close a child agent, bypassing the normal transition table."""
        registry = await self.get(db, child_id)
        if registry is None:
            raise ValueError(f"agent registry {child_id!r} not found")
        if registry.status != "CLOSED":
            changed_at = datetime.now(UTC)
            registry.status = "CLOSED"
            registry.status_changed_at = changed_at
            registry.closed_at = changed_at
            registry.force_closed = force_closed
            await db.flush()
        return registry

    async def list_active_children(self, db: AsyncSession, session_id: int) -> list[AgentRegistry]:
        """Return every child in this session not yet CLOSED, oldest-first."""
        stmt = (
            select(AgentRegistry)
            .where(AgentRegistry.session_id == session_id, AgentRegistry.status != "CLOSED")
            .order_by(AgentRegistry.created_at.asc(), AgentRegistry.child_id.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_for_session(
        self, db: AsyncSession, session_id: int
    ) -> list[AgentRegistry]:
        """Return every receipt in a session, including CLOSED rows."""
        stmt = (
            select(AgentRegistry)
            .where(AgentRegistry.session_id == session_id)
            .order_by(AgentRegistry.created_at.asc(), AgentRegistry.child_id.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_children(
        self, db: AsyncSession, session_id: int, parent_agent_id: str
    ) -> list[AgentRegistry]:
        """Return one receipt's direct children within its session."""
        stmt = (
            select(AgentRegistry)
            .where(
                AgentRegistry.session_id == session_id,
                AgentRegistry.parent_agent_id == parent_agent_id,
            )
            .order_by(AgentRegistry.created_at.asc(), AgentRegistry.child_id.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_descendants(
        self,
        db: AsyncSession,
        session_id: int,
        child_id: str,
        *,
        deepest_first: bool = False,
    ) -> list[AgentRegistry]:
        """Return all descendants, defensively stopping if corrupt rows form a cycle."""
        receipts = await self.list_for_session(db, session_id)
        by_parent: dict[str, list[AgentRegistry]] = {}
        for receipt in receipts:
            if receipt.parent_agent_id is not None:
                by_parent.setdefault(receipt.parent_agent_id, []).append(receipt)

        descendants: list[tuple[int, AgentRegistry]] = []
        visited = {child_id}
        pending = [(child_id, 0)]
        while pending:
            parent_id, parent_depth = pending.pop()
            for child in by_parent.get(parent_id, []):
                if child.child_id in visited:
                    continue
                visited.add(child.child_id)
                depth = parent_depth + 1
                descendants.append((depth, child))
                pending.append((child.child_id, depth))

        if deepest_first:
            descendants.sort(key=lambda item: (-item[0], item[1].created_at, item[1].child_id))
        else:
            descendants.sort(key=lambda item: (item[1].created_at, item[1].child_id))
        return [receipt for _depth, receipt in descendants]

    async def count_for_session(self, db: AsyncSession, session_id: int) -> int:
        """Count all receipts ever created in a session, including CLOSED rows."""
        stmt = select(func.count()).select_from(AgentRegistry).where(
            AgentRegistry.session_id == session_id
        )
        result = await db.execute(stmt)
        return int(result.scalar_one())

    async def list_active(self, db: AsyncSession) -> list[AgentRegistry]:
        """Return every non-CLOSED row across all sessions, oldest-first.

        Used by SpawnManager.reconcile_startup() at process boot, before any
        session context exists — unlike list_active_children, this is
        deliberately not scoped to one session_id.
        """
        stmt = (
            select(AgentRegistry)
            .where(AgentRegistry.status != "CLOSED")
            .order_by(AgentRegistry.created_at.asc(), AgentRegistry.child_id.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_terminal_before(
        self, db: AsyncSession, cutoff: datetime
    ) -> list[AgentRegistry]:
        """Return terminal receipts whose lifecycle clock is older than cutoff."""
        stmt = (
            select(AgentRegistry)
            .where(
                AgentRegistry.status.in_(_TERMINAL_STATUSES),
                AgentRegistry.status_changed_at < cutoff,
            )
            .order_by(AgentRegistry.created_at.asc(), AgentRegistry.child_id.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_snapshot_for_session(
        self,
        db: AsyncSession,
        session_id: int,
        *,
        terminal_limit: int = 20,
    ) -> list[AgentRegistry]:
        """返回快照所需的子 Agent：全部活跃项 + 最近若干已终结项。"""
        active_stmt = (
            select(AgentRegistry)
            .where(
                AgentRegistry.session_id == session_id,
                AgentRegistry.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(AgentRegistry.created_at.asc(), AgentRegistry.child_id.asc())
        )
        active = list((await db.execute(active_stmt)).scalars().all())
        terminal_stmt = (
            select(AgentRegistry)
            .where(
                AgentRegistry.session_id == session_id,
                AgentRegistry.status.in_(_TERMINAL_STATUSES | {"CLOSED"}),
            )
            .order_by(AgentRegistry.created_at.desc(), AgentRegistry.child_id.desc())
            .limit(terminal_limit)
        )
        recent_terminal = list((await db.execute(terminal_stmt)).scalars().all())
        recent_terminal.reverse()
        return active + recent_terminal

    async def list_created_since(
        self,
        db: AsyncSession,
        session_id: int,
        since: datetime,
    ) -> list[AgentRegistry]:
        """返回本会话在 `since` 之后创建的子 Agent。

        用于把一轮对话里派生出来的子 Agent 用量并进那一轮的合计。
        绝大多数轮次不派生子 Agent，这条查询走 session_id 索引后返回空集。
        """
        stmt = (
            select(AgentRegistry)
            .where(
                AgentRegistry.session_id == session_id,
                AgentRegistry.created_at >= since,
            )
            .order_by(AgentRegistry.created_at.asc(), AgentRegistry.child_id.asc())
        )
        return list((await db.execute(stmt)).scalars().all())

    async def reserved_cost_for_session(self, db: AsyncSession, session_id: int) -> float:
        """Return conservative reserved/actual child cost for one session."""
        receipts = await self.list_for_session(db, session_id)
        costs: list[float] = []
        for receipt in receipts:
            if receipt.status in _ACTIVE_STATUSES:
                key = "max_cost_usd"
            elif receipt.status in _ACTUAL_COST_STATUSES:
                key = "cost_used_usd"
            else:
                raise ValueError(f"unknown agent registry status {receipt.status!r}")

            if key not in receipt.budget:
                raise ValueError(f"agent budget {key!r} is required")
            value = receipt.budget[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value < 0
            ):
                raise ValueError(
                    f"agent budget {key!r} must be a finite non-negative number"
                )
            costs.append(float(value))
        return fsum(costs)


agent_registry_crud = CRUDAgentRegistry()
