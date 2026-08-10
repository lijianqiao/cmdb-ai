"""CRUD operations for agent observability trace events (append-only)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_trace_event import AgentTraceEvent


class CRUDAgentTraceEvent:
    """Append-only trace/span storage."""

    model = AgentTraceEvent

    async def record(
        self,
        db: AsyncSession,
        *,
        trace_id: str,
        session_id: int,
        agent_id: str,
        parent_agent_id: str | None,
        step: int,
        span_type: str,
        tool: str | None = None,
        control: str | None = None,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        error_class: str | None = None,
    ) -> AgentTraceEvent:
        """Append one trace event and flush."""
        event = AgentTraceEvent(
            trace_id=trace_id,
            session_id=session_id,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            step=step,
            span_type=span_type,
            tool=tool,
            control=control,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            error_class=error_class,
        )
        db.add(event)
        await db.flush()
        return event

    async def list_for_trace(self, db: AsyncSession, trace_id: str) -> list[AgentTraceEvent]:
        """Return every span for one trace, ordered by step."""
        stmt = (
            select(AgentTraceEvent)
            .where(AgentTraceEvent.trace_id == trace_id)
            .order_by(AgentTraceEvent.step.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


agent_trace_event_crud = CRUDAgentTraceEvent()
