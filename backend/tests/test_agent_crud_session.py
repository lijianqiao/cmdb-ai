"""CRUD tests for AgentSession."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.agent_session import AgentSession
from app.models.user import User

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def agent_session(db_session: AsyncSession, test_user: User) -> AgentSession:
    """创建一条测试用 Agent 会话。"""
    session = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "租约测试", "status": "active"}
    )
    await db_session.commit()
    return session


async def test_create_and_get(db_session: AsyncSession, test_user: User) -> None:
    session = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "巡检", "status": "active"}
    )
    await db_session.commit()

    fetched = await agent_session_crud.get(db_session, session.id)
    assert fetched is not None
    assert fetched.title == "巡检"


async def test_list_for_user_orders_newest_first_and_counts(
    db_session: AsyncSession, test_user: User
) -> None:
    first = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "第一次会话", "status": "active"}
    )
    await db_session.flush()
    second = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "第二次会话", "status": "active"}
    )
    await db_session.commit()

    items, total = await agent_session_crud.list_for_user(db_session, test_user.id)

    assert total == 2
    assert [item.id for item in items] == [second.id, first.id]


async def test_list_for_user_excludes_other_users(
    db_session: AsyncSession, test_user: User, superuser: User
) -> None:
    await agent_session_crud.create(
        db_session, {"user_id": superuser.id, "title": "别人的会话", "status": "active"}
    )
    await db_session.commit()

    items, total = await agent_session_crud.list_for_user(db_session, test_user.id)

    assert total == 0
    assert items == []


async def test_turn_lease_is_owner_token_guarded(db_session, agent_session) -> None:
    assert await agent_session_crud.claim_turn(db_session, agent_session.id, "token-a")
    assert not await agent_session_crud.claim_turn(db_session, agent_session.id, "token-b")
    assert not await agent_session_crud.release_turn(db_session, agent_session.id, "token-b")
    assert await agent_session_crud.release_turn(db_session, agent_session.id, "token-a")


async def test_recover_active_turns_clears_non_empty_leases(
    db_session: AsyncSession, test_user: User
) -> None:
    """启动恢复只清空遗留的非空租约，不影响空闲会话。"""
    busy = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "忙", "status": "active"}
    )
    idle = await agent_session_crud.create(
        db_session, {"user_id": test_user.id, "title": "闲", "status": "active"}
    )
    await db_session.commit()
    assert await agent_session_crud.claim_turn(db_session, busy.id, "stale-token")
    await db_session.commit()

    cleared = await agent_session_crud.recover_active_turns(db_session)
    await db_session.commit()

    assert cleared == 1
    refreshed_busy = await agent_session_crud.get(db_session, busy.id)
    refreshed_idle = await agent_session_crud.get(db_session, idle.id)
    assert refreshed_busy is not None
    assert refreshed_busy.active_turn_token is None
    assert refreshed_busy.active_turn_started_at is None
    assert refreshed_idle is not None
    assert refreshed_idle.active_turn_token is None


# ---------------------------------------------------------------------------
# R4：删除聊天改为归档。归档只是把会话从用户视野里收起来，提案与执行证据原样保留；
# 还有事情在跑（turn、子 Agent、待执行/执行中/结果不确定的提案）时拒绝归档；
# 还没审批的提案随归档撤回。
# ---------------------------------------------------------------------------


async def _proposal(db: AsyncSession, session_id: int, status: str) -> int:
    proposal = await hitl_proposal_crud.create(
        db,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_control",
        action_payload={"asset_id": 1, "command_name": "reboot"},
    )
    proposal.status = status
    await db.commit()
    return proposal.id


async def test_archive_hides_session_from_owner_list(
    db_session: AsyncSession, test_user: User, agent_session: AgentSession
) -> None:
    result = await agent_session_crud.archive(db_session, agent_session.id)
    await db_session.commit()

    assert result.outcome == "archived"
    items, total = await agent_session_crud.list_for_user(db_session, test_user.id)
    assert items == [] and total == 0
    archived = await agent_session_crud.get(db_session, agent_session.id)
    assert archived is not None and archived.status == "archived"


async def test_archive_refused_while_turn_active(
    db_session: AsyncSession, agent_session: AgentSession
) -> None:
    assert await agent_session_crud.claim_turn(db_session, agent_session.id, "turn-1")
    await db_session.commit()

    result = await agent_session_crud.archive(db_session, agent_session.id)
    assert result.outcome == "active_turn"
    still = await agent_session_crud.get(db_session, agent_session.id)
    assert still is not None and still.status == "active"


@pytest.mark.parametrize("status", ["APPROVED", "EXECUTING", "UNKNOWN"])
async def test_archive_refused_with_unsettled_proposal(
    db_session: AsyncSession, agent_session: AgentSession, status: str
) -> None:
    """待执行/执行中/结果不确定：聊天消失后就没人能继续核实了，先处理完再归档。"""
    await _proposal(db_session, agent_session.id, status)

    result = await agent_session_crud.archive(db_session, agent_session.id)
    assert result.outcome == "unsettled_proposals"


@pytest.mark.parametrize("status", ["EXECUTED", "REJECTED"])
async def test_archive_leaves_finished_proposals_untouched(
    db_session: AsyncSession, agent_session: AgentSession, status: str
) -> None:
    proposal_id = await _proposal(db_session, agent_session.id, status)

    result = await agent_session_crud.archive(db_session, agent_session.id)

    assert result.outcome == "archived"
    assert result.withdrawn_proposal_ids == ()
    finished = await hitl_proposal_crud.get(db_session, proposal_id)
    assert finished is not None and finished.status == status


async def test_archive_withdraws_pending_proposals(
    db_session: AsyncSession, test_user: User, agent_session: AgentSession
) -> None:
    """待审批的提案随归档撤回：否则聊天看不见了，提案却还能被批准、到设备上执行。

    也不能拒绝归档——没有审批权限的用户拒绝不了自己的提案，聊天就永远收不起来。
    """
    proposal_id = await _proposal(db_session, agent_session.id, "PENDING")

    result = await agent_session_crud.archive(db_session, agent_session.id)

    assert result.outcome == "archived"
    assert result.withdrawn_proposal_ids == (proposal_id,)
    withdrawn = await hitl_proposal_crud.get(db_session, proposal_id)
    assert withdrawn is not None
    assert withdrawn.status == "REJECTED"
    assert withdrawn.status_reason == "withdrawn_on_archive"
    assert withdrawn.reviewed_by_user_id == test_user.id
    assert withdrawn.reviewed_at is not None


async def test_archive_refused_with_running_child_agent(
    db_session: AsyncSession, agent_session: AgentSession
) -> None:
    child = await agent_registry_crud.create(
        db_session,
        session_id=agent_session.id,
        trace_id="trace-1",
        role_version="v1",
        parent_agent_id=None,
        agent_path="/root/child-1",
        role="kb_explorer",
        model="chat-fast",
        tools_allowlist=["kb_read"],
        sandbox_mode="read_only",
        task_brief="读文档",
        budget={},
    )
    child.status = "RUNNING"
    await db_session.commit()

    result = await agent_session_crud.archive(db_session, agent_session.id)
    assert result.outcome == "active_children"


async def test_archive_unknown_or_already_archived_session_is_not_found(
    db_session: AsyncSession, agent_session: AgentSession
) -> None:
    assert (await agent_session_crud.archive(db_session, 999999)).outcome == "not_found"
    assert (await agent_session_crud.archive(db_session, agent_session.id)).outcome == "archived"
    await db_session.commit()
    again = await agent_session_crud.archive(db_session, agent_session.id)
    assert again.outcome == "not_found"


async def test_claim_turn_refuses_archived_session(
    db_session: AsyncSession, agent_session: AgentSession
) -> None:
    """归档的会话不能再起新的一轮——否则归档之后还能长出新提案。"""
    await agent_session_crud.archive(db_session, agent_session.id)
    await db_session.commit()

    assert await agent_session_crud.claim_turn(db_session, agent_session.id, "turn-2") is False
