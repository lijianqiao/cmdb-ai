"""CRUD tests for HitlProposal — the PENDING/APPROVED/REJECTED/EXECUTED state machine."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.crud.agent_session import agent_session_crud
from app.crud.hitl_proposal import InvalidHitlTransitionError, hitl_proposal_crud
from app.models.hitl_proposal import HitlProposal
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


async def _approved_proposal(
    db_session: AsyncSession,
    test_user: User,
) -> HitlProposal:
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "test"},
    )
    await hitl_proposal_crud.decide(
        db_session,
        proposal.id,
        approve=True,
        reviewed_by_user_id=test_user.id,
    )
    return proposal


async def test_execution_state_machine_requires_claim(
    db_session: AsyncSession, test_user: User
) -> None:
    proposal = await _approved_proposal(db_session, test_user)

    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.mark_executed(db_session, proposal.id)

    executing = await hitl_proposal_crud.claim_execution(db_session, proposal.id)
    assert executing.status == "EXECUTING"
    assert executing.execution_started_at is not None

    executed = await hitl_proposal_crud.mark_executed(db_session, proposal.id)
    assert executed.status == "EXECUTED"
    assert executed.status_reason == "executor_succeeded"


async def test_claim_execution_rejects_second_claim(
    db_session: AsyncSession, test_user: User
) -> None:
    proposal = await _approved_proposal(db_session, test_user)
    await hitl_proposal_crud.claim_execution(db_session, proposal.id)

    with pytest.raises(InvalidHitlTransitionError) as exc_info:
        await hitl_proposal_crud.claim_execution(db_session, proposal.id)
    assert exc_info.value.current == "EXECUTING"


async def test_claim_execution_is_compare_and_swap(
    db_session: AsyncSession, db_engine: AsyncEngine, test_user: User
) -> None:
    proposal = await _approved_proposal(db_session, test_user)
    await db_session.commit()

    first = await hitl_proposal_crud.claim_execution(db_session, proposal.id)
    await db_session.commit()
    assert first.status == "EXECUTING"

    session_factory = async_sessionmaker(
        db_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as second_db_session:
        with pytest.raises(InvalidHitlTransitionError) as exc_info:
            await hitl_proposal_crud.claim_execution(second_db_session, proposal.id)
        assert exc_info.value.current == "EXECUTING"


async def test_reject_for_policy_transitions_approved_to_rejected(
    db_session: AsyncSession, test_user: User
) -> None:
    proposal = await _approved_proposal(db_session, test_user)
    rejected = await hitl_proposal_crud.reject_for_policy(db_session, proposal.id)

    assert rejected.status == "REJECTED"
    assert rejected.status_reason == "policy_blacklisted"


async def test_mark_unknown_transitions_executing_to_unknown(
    db_session: AsyncSession, test_user: User
) -> None:
    proposal = await _approved_proposal(db_session, test_user)
    await hitl_proposal_crud.claim_execution(db_session, proposal.id)
    unknown = await hitl_proposal_crud.mark_unknown(
        db_session, proposal.id, reason="dispatch_outcome_unknown"
    )

    assert unknown.status == "UNKNOWN"
    assert unknown.status_reason == "dispatch_outcome_unknown"


async def test_resolve_unknown_confirm_executed(
    db_session: AsyncSession, test_user: User
) -> None:
    proposal = await _approved_proposal(db_session, test_user)
    await hitl_proposal_crud.claim_execution(db_session, proposal.id)
    await hitl_proposal_crud.mark_unknown(
        db_session, proposal.id, reason="dispatch_outcome_unknown"
    )

    resolved = await hitl_proposal_crud.resolve_unknown(
        db_session,
        proposal.id,
        resolution="confirm_executed",
        resolved_by_user_id=test_user.id,
    )

    assert resolved.status == "EXECUTED"
    assert resolved.status_reason == "manual_confirmed"
    assert resolved.executed_at is not None
    assert resolved.resolved_by_user_id == test_user.id
    assert resolved.resolved_at is not None


async def test_resolve_unknown_allow_retry(
    db_session: AsyncSession, test_user: User
) -> None:
    proposal = await _approved_proposal(db_session, test_user)
    await hitl_proposal_crud.claim_execution(db_session, proposal.id)
    await hitl_proposal_crud.mark_unknown(
        db_session, proposal.id, reason="dispatch_outcome_unknown"
    )

    resolved = await hitl_proposal_crud.resolve_unknown(
        db_session,
        proposal.id,
        resolution="allow_retry",
        resolved_by_user_id=test_user.id,
    )

    assert resolved.status == "APPROVED"
    assert resolved.status_reason == "retry_authorized"
    assert resolved.execution_started_at is None
    assert resolved.resolved_by_user_id == test_user.id
    assert resolved.resolved_at is not None


async def test_resolve_unknown_rejects_non_unknown(
    db_session: AsyncSession, test_user: User
) -> None:
    proposal = await _approved_proposal(db_session, test_user)

    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.resolve_unknown(
            db_session,
            proposal.id,
            resolution="allow_retry",
            resolved_by_user_id=test_user.id,
        )


async def test_recover_executing_only_modifies_executing(
    db_session: AsyncSession, test_user: User
) -> None:
    approved = await _approved_proposal(db_session, test_user)

    executing_proposal = await _approved_proposal(db_session, test_user)
    await hitl_proposal_crud.claim_execution(db_session, executing_proposal.id)

    executed_proposal = await _approved_proposal(db_session, test_user)
    await hitl_proposal_crud.claim_execution(db_session, executed_proposal.id)
    await hitl_proposal_crud.mark_executed(db_session, executed_proposal.id)

    changed = await hitl_proposal_crud.recover_executing(db_session)
    assert changed == 1

    persisted_approved = await hitl_proposal_crud.get(db_session, approved.id)
    persisted_executing = await hitl_proposal_crud.get(db_session, executing_proposal.id)
    persisted_executed = await hitl_proposal_crud.get(db_session, executed_proposal.id)

    assert persisted_approved is not None
    assert persisted_approved.status == "APPROVED"
    assert persisted_executing is not None
    assert persisted_executing.status == "UNKNOWN"
    assert persisted_executed is not None
    assert persisted_executed.status == "EXECUTED"


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
    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.mark_executed(db_session, proposal.id)

    await hitl_proposal_crud.claim_execution(db_session, proposal.id)
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
