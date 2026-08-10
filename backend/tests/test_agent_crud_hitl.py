"""CRUD tests for HitlProposal — the PENDING/APPROVED/REJECTED/EXECUTED state machine."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_session import agent_session_crud
from app.crud.hitl_proposal import InvalidHitlTransitionError, hitl_proposal_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def test_create_starts_pending(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)

    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "SW-12 离线"},
    )
    await db_session.commit()

    assert proposal.status == "PENDING"


async def test_approve_sets_reviewer_and_timestamp(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={},
    )
    await db_session.flush()

    approved = await hitl_proposal_crud.decide(
        db_session, proposal.id, approve=True, reviewed_by_user_id=test_user.id
    )
    await db_session.commit()

    assert approved.status == "APPROVED"
    assert approved.reviewed_by_user_id == test_user.id
    assert approved.reviewed_at is not None


async def test_reject_sets_reviewer_and_timestamp(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={},
    )
    await db_session.flush()

    rejected = await hitl_proposal_crud.decide(
        db_session, proposal.id, approve=False, reviewed_by_user_id=test_user.id
    )
    await db_session.commit()

    assert rejected.status == "REJECTED"
    assert rejected.reviewed_by_user_id == test_user.id
    assert rejected.reviewed_at is not None


async def test_cannot_decide_twice(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={},
    )
    await db_session.flush()
    await hitl_proposal_crud.decide(
        db_session, proposal.id, approve=True, reviewed_by_user_id=test_user.id
    )

    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.decide(
            db_session, proposal.id, approve=False, reviewed_by_user_id=test_user.id
        )


async def test_cannot_decide_twice_after_rejection(
    db_session: AsyncSession, test_user: User
) -> None:
    """Rejected proposals are also terminal for decide() — not just approved ones."""
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={},
    )
    await db_session.flush()
    rejected = await hitl_proposal_crud.decide(
        db_session, proposal.id, approve=False, reviewed_by_user_id=test_user.id
    )
    assert rejected.status == "REJECTED"

    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.decide(
            db_session, proposal.id, approve=True, reviewed_by_user_id=test_user.id
        )


async def test_mark_executed_requires_approved_first(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={},
    )
    await db_session.flush()

    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.mark_executed(db_session, proposal.id)

    await hitl_proposal_crud.decide(
        db_session, proposal.id, approve=True, reviewed_by_user_id=test_user.id
    )
    executed = await hitl_proposal_crud.mark_executed(db_session, proposal.id)
    await db_session.commit()

    assert executed.status == "EXECUTED"
    assert executed.executed_at is not None

    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.mark_executed(db_session, proposal.id)


async def test_mark_executed_after_rejection_raises(
    db_session: AsyncSession, test_user: User
) -> None:
    """A REJECTED proposal must never reach EXECUTED — same terminal guard as decide()."""
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={},
    )
    await db_session.flush()
    rejected = await hitl_proposal_crud.decide(
        db_session, proposal.id, approve=False, reviewed_by_user_id=test_user.id
    )
    assert rejected.status == "REJECTED"

    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.mark_executed(db_session, proposal.id)
