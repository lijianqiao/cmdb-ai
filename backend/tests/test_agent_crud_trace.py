"""CRUD tests for AgentTraceEvent (append-only observability log)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def test_record_and_list_ordered_by_step(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)

    await agent_trace_event_crud.record(
        db_session,
        trace_id="trace-1",
        session_id=session_id,
        agent_id="root",
        parent_agent_id=None,
        step=2,
        span_type="tool",
        tool="kb_grep",
        control="ok",
        cost_usd=0.001,
        latency_ms=80,
    )
    await agent_trace_event_crud.record(
        db_session,
        trace_id="trace-1",
        session_id=session_id,
        agent_id="root",
        parent_agent_id=None,
        step=1,
        span_type="generation",
    )
    await db_session.commit()

    events = await agent_trace_event_crud.list_for_trace(db_session, "trace-1")

    assert [e.step for e in events] == [1, 2]
    assert events[1].tool == "kb_grep"


async def test_list_for_trace_excludes_other_traces(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await agent_trace_event_crud.record(
        db_session,
        trace_id="trace-a",
        session_id=session_id,
        agent_id="root",
        parent_agent_id=None,
        step=1,
        span_type="generation",
    )
    await agent_trace_event_crud.record(
        db_session,
        trace_id="trace-b",
        session_id=session_id,
        agent_id="root",
        parent_agent_id=None,
        step=1,
        span_type="generation",
    )
    await db_session.commit()

    events = await agent_trace_event_crud.list_for_trace(db_session, "trace-a")

    assert len(events) == 1
