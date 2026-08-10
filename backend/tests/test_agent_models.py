"""Structural tests for the new agent-runtime ORM models."""

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_message import AgentMessage
from app.models.agent_registry import AgentRegistry
from app.models.agent_session import AgentSession
from app.models.agent_trace_event import AgentTraceEvent
from app.models.hitl_proposal import HitlProposal
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_agent_session_round_trip(db_session: AsyncSession, test_user: User) -> None:
    session = AgentSession(user_id=test_user.id, title="网段巡检", status="active")
    db_session.add(session)
    await db_session.commit()

    result = await db_session.execute(select(AgentSession).where(AgentSession.id == session.id))
    stored = result.scalar_one()
    assert stored.status == "active"
    assert stored.user_id == test_user.id


async def test_agent_message_stores_tool_calls_json(
    db_session: AsyncSession, test_user: User
) -> None:
    session = AgentSession(user_id=test_user.id, title="", status="active")
    db_session.add(session)
    await db_session.flush()

    message = AgentMessage(
        session_id=session.id,
        role="assistant",
        content="",
        tool_calls=[{"id": "call_1", "name": "kb_grep", "arguments": "{}"}],
    )
    db_session.add(message)
    await db_session.commit()

    result = await db_session.execute(select(AgentMessage).where(AgentMessage.id == message.id))
    stored = result.scalar_one()
    assert stored.tool_calls == [{"id": "call_1", "name": "kb_grep", "arguments": "{}"}]
    assert stored.tool_call_id is None


async def test_agent_registry_defaults_to_requested_status(
    db_session: AsyncSession, test_user: User
) -> None:
    session = AgentSession(user_id=test_user.id, title="", status="active")
    db_session.add(session)
    await db_session.flush()

    child = AgentRegistry(
        session_id=session.id,
        parent_agent_id=None,
        agent_path="/root/kb_explorer",
        role="kb_explorer",
        model="local-chat",
        tools_allowlist=["kb_grep", "kb_read"],
        sandbox_mode="read-only",
        task_brief="查找 SOP 中关于交换机重启的章节",
        budget={"max_steps": 10, "max_cost_usd": 0.5},
    )
    db_session.add(child)
    await db_session.commit()

    result = await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.child_id == child.child_id)
    )
    stored = result.scalar_one()
    assert stored.status == "REQUESTED"
    assert stored.closed_at is None
    assert stored.tools_allowlist == ["kb_grep", "kb_read"]


async def test_hitl_proposal_defaults_to_pending(db_session: AsyncSession, test_user: User) -> None:
    session = AgentSession(user_id=test_user.id, title="", status="active")
    db_session.add(session)
    await db_session.flush()

    proposal = HitlProposal(
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "交换机 SW-12 离线"},
    )
    db_session.add(proposal)
    await db_session.commit()

    result = await db_session.execute(select(HitlProposal).where(HitlProposal.id == proposal.id))
    stored = result.scalar_one()
    assert stored.status == "PENDING"
    assert stored.reviewed_at is None


async def test_agent_trace_event_records_span(db_session: AsyncSession, test_user: User) -> None:
    session = AgentSession(user_id=test_user.id, title="", status="active")
    db_session.add(session)
    await db_session.flush()

    event = AgentTraceEvent(
        trace_id="trace-1",
        session_id=session.id,
        agent_id="root",
        parent_agent_id=None,
        step=1,
        span_type="tool",
        tool="kb_grep",
        control="ok",
        cost_usd=0.001,
        latency_ms=120,
    )
    db_session.add(event)
    await db_session.commit()

    result = await db_session.execute(
        select(AgentTraceEvent).where(AgentTraceEvent.trace_id == "trace-1")
    )
    stored = result.scalar_one()
    assert stored.span_type == "tool"
    assert stored.error_class is None
    assert isinstance(stored.created_at, datetime)
    assert stored.created_at.tzinfo is not None
