"""Structural tests for the new agent-runtime ORM models."""

from datetime import datetime
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.hitl_execution_result import hitl_execution_result_crud
from app.models.agent_message import AgentMessage
from app.models.agent_registry import AgentRegistry
from app.models.agent_session import AgentSession
from app.models.agent_trace_event import AgentTraceEvent
from app.models.hitl_execution_result import HitlExecutionResult
from app.models.hitl_proposal import HitlProposal
from app.models.user import User


@pytest.mark.asyncio
async def test_agent_session_round_trip(db_session: AsyncSession, test_user: User) -> None:
    session = AgentSession(user_id=test_user.id, title="网段巡检", status="active")
    db_session.add(session)
    await db_session.commit()

    result = await db_session.execute(select(AgentSession).where(AgentSession.id == session.id))
    stored = result.scalar_one()
    assert stored.status == "active"
    assert stored.user_id == test_user.id
    assert stored.memory_summary is None
    assert stored.compacted_through_message_id is None


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
        role_version="2026-08-11",
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
    assert UUID(stored.trace_id).version == 4
    assert stored.role_version == "2026-08-11"
    assert stored.status_changed_at.tzinfo is not None
    assert stored.force_closed is False


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


def test_hitl_execution_recovery_columns_exist() -> None:
    """HITL 执行恢复与 UNKNOWN 处置字段应存在于 ORM 表定义中。"""
    columns = HitlProposal.__table__.columns
    assert columns["execution_started_at"].nullable is True
    assert columns["status_reason"].type.length == 50
    assert columns["resolved_by_user_id"].foreign_keys
    assert columns["resolved_at"].nullable is True


def test_agent_session_turn_lease_columns_exist() -> None:
    """根会话 turn 租约字段应存在于 ORM 表定义中。"""
    columns = AgentSession.__table__.columns
    assert columns["active_turn_token"].type.length == 36
    assert columns["active_turn_started_at"].nullable is True


def test_hitl_execution_result_schema() -> None:
    columns = HitlExecutionResult.__table__.columns
    assert columns["proposal_id"].unique is True
    assert columns["content"].nullable is False
    assert columns["content_length"].nullable is False
    assert columns["summary_status"].type.length == 20
    proposal_fk = next(iter(columns["proposal_id"].foreign_keys))
    assert proposal_fk.target_fullname == "hitl_proposals.id"
    assert proposal_fk.ondelete == "CASCADE"


@pytest.mark.asyncio
async def test_hitl_execution_result_crud_creates_once_and_calculates_content_length(
    db_session: AsyncSession, test_user: User
) -> None:
    session = AgentSession(user_id=test_user.id, title="", status="active")
    db_session.add(session)
    await db_session.flush()
    proposal = HitlProposal(
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="query",
        action_payload={"command": "show version"},
    )
    db_session.add(proposal)
    await db_session.flush()

    created = await hitl_execution_result_crud.create_for_proposal(
        db_session,
        proposal_id=proposal.id,
        content="设备型号：S9300",
    )
    duplicate = await hitl_execution_result_crud.create_for_proposal(
        db_session,
        proposal_id=proposal.id,
        content="ignored",
    )

    assert created.proposal_id == proposal.id
    assert created.content_length == 10
    assert created.summary_status == "pending"
    assert duplicate is created
    assert duplicate.content == "设备型号：S9300"


@pytest.mark.asyncio
async def test_hitl_execution_result_crud_returns_existing_proposal_ids(
    db_session: AsyncSession, test_user: User
) -> None:
    session = AgentSession(user_id=test_user.id, title="", status="active")
    db_session.add(session)
    await db_session.flush()
    proposals = [
        HitlProposal(
            session_id=session.id,
            proposed_by_agent_id=None,
            action_type="query",
            action_payload={"command": command},
        )
        for command in ("show version", "show interfaces")
    ]
    db_session.add_all(proposals)
    await db_session.flush()
    await hitl_execution_result_crud.create_for_proposal(
        db_session,
        proposal_id=proposals[0].id,
        content="output",
    )

    existing = await hitl_execution_result_crud.existing_proposal_ids(
        db_session,
        [proposals[0].id, proposals[1].id, 999],
    )

    assert existing == {proposals[0].id}
    assert await hitl_execution_result_crud.existing_proposal_ids(db_session, []) == set()
