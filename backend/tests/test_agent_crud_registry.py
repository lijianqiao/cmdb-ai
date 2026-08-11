"""CRUD tests for AgentRegistry — the ChildReceipt store and its state machine."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_registry import InvalidAgentStatusTransitionError, agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def _spawn(
    db_session: AsyncSession,
    session_id: int,
    *,
    child_id: str | None = None,
    parent_agent_id: str | None = None,
    budget: dict[str, object] | None = None,
) -> str:
    child = await agent_registry_crud.create(
        db_session,
        session_id=session_id,
        child_id=child_id,
        parent_agent_id=parent_agent_id,
        agent_path="/root/kb_explorer",
        trace_id=f"trace-{child_id or 'generated'}",
        role="kb_explorer",
        role_version="2026-08-11",
        model="local-chat",
        tools_allowlist=["kb_grep"],
        sandbox_mode="read-only",
        task_brief="找一下重启流程",
        budget=budget or {"max_steps": 5},
    )
    await db_session.commit()
    return child.child_id


async def test_create_starts_requested(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)

    fetched = await agent_registry_crud.get(db_session, child_id)
    assert fetched is not None
    assert fetched.status == "REQUESTED"


async def test_valid_transition_chain(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)

    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")
    await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")
    updated = await agent_registry_crud.transition_status(
        db_session, child_id, "COMPLETED", result_summary="找到了,在 SOP 第 3 章"
    )
    await db_session.commit()

    assert updated.status == "COMPLETED"
    assert updated.result_summary == "找到了,在 SOP 第 3 章"


async def test_illegal_transition_raises(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)

    with pytest.raises(InvalidAgentStatusTransitionError):
        await agent_registry_crud.transition_status(db_session, child_id, "COMPLETED")


async def test_close_is_idempotent_and_force_detaches_running(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")
    await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")

    first_close = await agent_registry_crud.close(db_session, child_id)
    assert first_close.status == "CLOSED"
    assert first_close.closed_at is not None

    second_close = await agent_registry_crud.close(db_session, child_id)
    assert second_close.status == "CLOSED"
    assert second_close.closed_at == first_close.closed_at


async def test_list_active_children_excludes_closed(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    still_running = await _spawn(db_session, session_id)
    closed_one = await _spawn(db_session, session_id)
    await agent_registry_crud.close(db_session, closed_one)
    await db_session.commit()

    active = await agent_registry_crud.list_active_children(db_session, session_id)

    assert [c.child_id for c in active] == [still_running]


async def test_transition_to_failed(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)

    updated = await agent_registry_crud.transition_status(
        db_session, child_id, "FAILED", result_summary="工具调用超时,任务失败"
    )
    await db_session.commit()

    assert updated.status == "FAILED"


async def test_transition_to_cancelled(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")
    await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")

    updated = await agent_registry_crud.transition_status(db_session, child_id, "CANCELLED")
    await db_session.commit()

    assert updated.status == "CANCELLED"


async def test_illegal_transition_from_spawning_raises(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")

    with pytest.raises(InvalidAgentStatusTransitionError):
        await agent_registry_crud.transition_status(db_session, child_id, "COMPLETED")


async def test_illegal_transition_from_terminal_status_raises(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")
    await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")
    await agent_registry_crud.transition_status(db_session, child_id, "COMPLETED")

    with pytest.raises(InvalidAgentStatusTransitionError):
        await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")


async def test_illegal_transition_from_closed_raises(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    await agent_registry_crud.close(db_session, child_id)

    with pytest.raises(InvalidAgentStatusTransitionError):
        await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")


async def test_close_from_spawning_succeeds(db_session: AsyncSession, test_user: User) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")

    closed = await agent_registry_crud.close(db_session, child_id)
    await db_session.commit()

    assert closed.status == "CLOSED"


async def test_close_from_terminal_status_succeeds(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    await agent_registry_crud.transition_status(
        db_session, child_id, "FAILED", result_summary="连续三次工具调用失败"
    )

    closed = await agent_registry_crud.close(db_session, child_id)
    await db_session.commit()

    assert closed.status == "CLOSED"


async def test_status_transition_updates_status_changed_at(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    registry = await agent_registry_crud.get(db_session, child_id)
    assert registry is not None
    original = datetime(2020, 1, 1, tzinfo=UTC)
    registry.status_changed_at = original
    await db_session.flush()

    updated = await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")

    assert updated.status_changed_at > original


async def test_force_close_is_idempotent_and_structured(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")
    await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")

    first = await agent_registry_crud.close(db_session, child_id, force_closed=True)
    first_clock = first.status_changed_at
    first_closed_at = first.closed_at
    second = await agent_registry_crud.close(db_session, child_id, force_closed=False)

    assert second.status == "CLOSED"
    assert second.force_closed is True
    assert second.result_summary is None
    assert second.status_changed_at == first_clock
    assert second.closed_at == first_closed_at


async def test_list_for_session_is_stable_and_includes_closed(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    later_id = await _spawn(db_session, session_id, child_id="child-b")
    earlier_id = await _spawn(db_session, session_id, child_id="child-a")
    later = await agent_registry_crud.get(db_session, later_id)
    earlier = await agent_registry_crud.get(db_session, earlier_id)
    assert later is not None and earlier is not None
    created_at = datetime.now(UTC)
    later.created_at = created_at
    earlier.created_at = created_at
    await agent_registry_crud.close(db_session, later_id)

    receipts = await agent_registry_crud.list_for_session(db_session, session_id)

    assert [receipt.child_id for receipt in receipts] == [earlier_id, later_id]
    assert receipts[1].status == "CLOSED"


async def test_list_descendants_returns_deepest_first(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    root = await _spawn(db_session, session_id, child_id="root-child")
    child = await _spawn(
        db_session, session_id, child_id="level-1", parent_agent_id=root
    )
    grandchild = await _spawn(
        db_session, session_id, child_id="level-2", parent_agent_id=child
    )

    descendants = await agent_registry_crud.list_descendants(
        db_session, session_id, root, deepest_first=True
    )

    assert [receipt.child_id for receipt in descendants] == [grandchild, child]


async def test_list_descendants_defends_against_cycles(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    root = await _spawn(db_session, session_id, child_id="cycle-root")
    child = await _spawn(
        db_session, session_id, child_id="cycle-child", parent_agent_id=root
    )
    root_receipt = await agent_registry_crud.get(db_session, root)
    assert root_receipt is not None
    root_receipt.parent_agent_id = child
    await db_session.flush()

    descendants = await agent_registry_crud.list_descendants(
        db_session, session_id, root, deepest_first=True
    )

    assert [receipt.child_id for receipt in descendants] == [child]


async def test_count_for_session_is_cumulative(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await _spawn(db_session, session_id)
    closed_id = await _spawn(db_session, session_id)
    await agent_registry_crud.close(db_session, closed_id)

    assert await agent_registry_crud.count_for_session(db_session, session_id) == 2


async def test_list_terminal_before_uses_status_changed_at(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    old_id = await _spawn(db_session, session_id, child_id="old-terminal")
    new_id = await _spawn(db_session, session_id, child_id="new-terminal")
    active_id = await _spawn(db_session, session_id, child_id="old-active")
    closed_id = await _spawn(db_session, session_id, child_id="old-closed")
    await agent_registry_crud.transition_status(db_session, old_id, "FAILED")
    await agent_registry_crud.transition_status(db_session, new_id, "FAILED")
    await agent_registry_crud.close(db_session, closed_id)
    old = await agent_registry_crud.get(db_session, old_id)
    new = await agent_registry_crud.get(db_session, new_id)
    active = await agent_registry_crud.get(db_session, active_id)
    closed = await agent_registry_crud.get(db_session, closed_id)
    assert old is not None and new is not None and active is not None and closed is not None
    cutoff = datetime.now(UTC)
    old.status_changed_at = cutoff - timedelta(seconds=2)
    new.status_changed_at = cutoff + timedelta(seconds=2)
    active.status_changed_at = cutoff - timedelta(seconds=2)
    closed.status_changed_at = cutoff - timedelta(seconds=2)
    await db_session.flush()

    receipts = await agent_registry_crud.list_terminal_before(db_session, cutoff)

    assert [receipt.child_id for receipt in receipts] == [old_id]


async def test_reserved_cost_uses_active_max_and_terminal_actual(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    running_id = await _spawn(
        db_session,
        session_id,
        budget={"max_cost_usd": 1.0, "cost_used_usd": 0.2},
    )
    completed_id = await _spawn(
        db_session,
        session_id,
        budget={"max_cost_usd": 1.0, "cost_used_usd": 0.3},
    )
    closed_id = await _spawn(
        db_session,
        session_id,
        budget={"max_cost_usd": 1.0, "cost_used_usd": 0.4},
    )
    for child_id in (running_id, completed_id, closed_id):
        await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")
        await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")
    await agent_registry_crud.transition_status(db_session, completed_id, "COMPLETED")
    await agent_registry_crud.transition_status(db_session, closed_id, "COMPLETED")
    await agent_registry_crud.close(db_session, closed_id)

    assert await agent_registry_crud.reserved_cost_for_session(db_session, session_id) == 1.7


@pytest.mark.parametrize(
    ("status", "budget", "message"),
    [
        ("RUNNING", {"cost_used_usd": 0.2}, "max_cost_usd"),
        ("RUNNING", {"max_cost_usd": True}, "max_cost_usd"),
        ("COMPLETED", {"cost_used_usd": -0.1}, "cost_used_usd"),
        ("FAILED", {"cost_used_usd": float("nan")}, "cost_used_usd"),
        ("CANCELLED", {"cost_used_usd": float("inf")}, "cost_used_usd"),
        ("CLOSED", {"cost_used_usd": float("-inf")}, "cost_used_usd"),
    ],
)
async def test_reserved_cost_rejects_invalid_selected_budget_value(
    db_session: AsyncSession,
    test_user: User,
    status: str,
    budget: dict[str, object],
    message: str,
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    receipt = await agent_registry_crud.get(db_session, child_id)
    assert receipt is not None
    receipt.status = status
    receipt.budget = budget
    await db_session.flush()

    with pytest.raises(ValueError, match=message):
        await agent_registry_crud.reserved_cost_for_session(db_session, session_id)


async def test_reserved_cost_rejects_unknown_status(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    receipt = await agent_registry_crud.get(db_session, child_id)
    assert receipt is not None
    receipt.status = "CORRUPT"
    await db_session.flush()

    with pytest.raises(ValueError, match="unknown agent registry status"):
        await agent_registry_crud.reserved_cost_for_session(db_session, session_id)


async def test_terminal_transition_flushes_complete_receipt_atomically(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    child_id = await _spawn(db_session, session_id)
    await agent_registry_crud.transition_status(db_session, child_id, "SPAWNING")
    await agent_registry_crud.transition_status(db_session, child_id, "RUNNING")
    budget = {
        "max_steps": 20,
        "max_cost_usd": 1.0,
        "max_wall_time_seconds": 120.0,
        "steps_used": 4,
        "cost_used_usd": 0.25,
    }

    receipt = await agent_registry_crud.transition_status(
        db_session,
        child_id,
        "COMPLETED",
        budget=budget,
        result_summary="done",
        artifacts=["artifact.txt"],
    )

    assert receipt.budget == budget
    assert receipt.result_summary == "done"
    assert receipt.artifacts == ["artifact.txt"]
