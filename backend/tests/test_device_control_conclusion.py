"""设备变更结论：逐口列出做了什么，并提示同一轮因等审批被跳过的操作。"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent import hitl_gate
from app.agent.hitl import ProposalSafeSummary
from app.agent.hitl_executor import (
    build_device_control_conclusion,
    deliver_device_control_conclusion,
)
from app.agent.loop import SKIPPED_TOOL_RESULT
from app.crud.agent_message import agent_message_crud
from app.crud.agent_session import agent_session_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.agent_message import AgentMessage
from app.models.hitl_proposal import HitlProposal
from app.models.user import User

pytestmark = pytest.mark.asyncio

_SNAPSHOT = {"asset": {"id": 1, "hostname": "SW-01", "ip_address": "10.0.30.1"}}


def _proposal(status: str, payload: dict[str, object]) -> HitlProposal:
    return HitlProposal(
        session_id=1,
        action_type="device_control",
        action_payload={"asset_id": 1, "proposal_reason": "测试", **payload},
        status=status,
        evidence_snapshot=_SNAPSHOT,
    )


async def test_executed_conclusion_lists_every_interface() -> None:
    proposal = _proposal(
        "EXECUTED",
        {"command_name": "port_disable", "interface_names": ["Gi1/0/15", "Gi1/0/16"]},
    )

    content = build_device_control_conclusion(proposal)

    assert content is not None
    assert content.startswith(
        "已在 SW-01（10.0.30.1）上关闭端口 Gi1/0/15、Gi1/0/16，设备已接受命令。"
    )


async def test_conclusion_still_reads_old_single_interface_payload() -> None:
    proposal = _proposal(
        "EXECUTED", {"command_name": "port_enable", "interface_name": "GigabitEthernet0/1"}
    )

    content = build_device_control_conclusion(proposal)

    assert content is not None
    assert "开启端口 GigabitEthernet0/1" in content


async def test_unknown_conclusion_carries_the_per_interface_outcome() -> None:
    outcome = "已下发：Gi1/0/15；Gi1/0/16 被设备拒绝（% Invalid input）；未下发：Gi1/0/17"
    proposal = _proposal(
        "UNKNOWN",
        {
            "command_name": "port_disable",
            "interface_names": ["Gi1/0/15", "Gi1/0/16", "Gi1/0/17"],
            "last_error": outcome,
        },
    )

    content = build_device_control_conclusion(proposal)

    assert content is not None
    assert outcome in content


async def test_conclusion_mentions_calls_skipped_while_waiting_for_approval() -> None:
    proposal = _proposal("EXECUTED", {"command_name": "reboot"})

    content = build_device_control_conclusion(proposal, skipped_calls=2)

    assert content is not None
    assert "本轮还有 2 个操作因等待审批被跳过" in content


async def _executed_proposal_in_session(db: AsyncSession, user: User) -> tuple[int, int]:
    session = await agent_session_crud.create(
        db, {"user_id": user.id, "title": "批量关口", "status": "active"}
    )
    proposal = await hitl_proposal_crud.create(
        db,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="device_control",
        action_payload={
            "asset_id": 1,
            "command_name": "port_disable",
            "interface_names": ["Gi1/0/15"],
            "proposal_reason": "关口",
        },
        evidence_snapshot=_SNAPSHOT,
    )
    proposal.status = "EXECUTED"
    await db.flush()
    return session.id, proposal.id


def _pending_content(proposal_id: int) -> str:
    """用门控真实生成的「等待审批」文案，而不是在测试里再抄一遍格式。"""
    summary = ProposalSafeSummary(
        proposal_id=proposal_id,
        action_type="device_control",
        status="PENDING",
        reason="关口",
        asset_id=1,
    )
    return hitl_gate._pending_result(summary, "device_control").content


async def _append_round(
    db: AsyncSession, session_id: int, calls: list[tuple[str, str]]
) -> None:
    """模拟 loop 写下的一轮：一条带 tool_calls 的助手消息，后面跟每个调用的结果。"""
    await agent_message_crud.append(
        db,
        session_id=session_id,
        role="assistant",
        content="",
        tool_calls=[
            {"id": call_id, "name": "device_control", "arguments": "{}"} for call_id, _ in calls
        ],
    )
    for call_id, content in calls:
        await agent_message_crud.append(
            db, session_id=session_id, role="tool", content=content, tool_call_id=call_id
        )


async def _latest_assistant_text(db_engine: AsyncEngine, session_id: int) -> str:
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as db:
        row = (
            await db.execute(
                select(AgentMessage)
                .where(AgentMessage.session_id == session_id, AgentMessage.role == "assistant")
                .order_by(AgentMessage.id.desc())
                .limit(1)
            )
        ).scalar_one()
        return row.content


async def test_delivered_conclusion_counts_calls_skipped_in_the_same_round(
    db_engine: AsyncEngine, db_session: AsyncSession, test_user: User
) -> None:
    """人批准后结论里要提醒：同一轮还有几个操作因等审批被跳过，需要的话请再发一次。"""
    session_id, proposal_id = await _executed_proposal_in_session(db_session, test_user)
    # 更早的一轮也有被跳过的调用，不能算进这一次。
    await _append_round(
        db_session,
        session_id,
        [("old-1", _pending_content(proposal_id + 1000)), ("old-2", SKIPPED_TOOL_RESULT)],
    )
    await _append_round(
        db_session,
        session_id,
        [
            ("call-1", _pending_content(proposal_id)),
            ("call-2", SKIPPED_TOOL_RESULT),
            ("call-3", SKIPPED_TOOL_RESULT),
        ],
    )
    await db_session.commit()

    await deliver_device_control_conclusion(
        async_sessionmaker(db_engine, expire_on_commit=False), proposal_id
    )

    content = await _latest_assistant_text(db_engine, session_id)
    assert content.startswith("已在 SW-01（10.0.30.1）上关闭端口 Gi1/0/15")
    assert "本轮还有 2 个操作因等待审批被跳过" in content


async def test_delivered_conclusion_has_no_skip_hint_when_nothing_was_skipped(
    db_engine: AsyncEngine, db_session: AsyncSession, test_user: User
) -> None:
    session_id, proposal_id = await _executed_proposal_in_session(db_session, test_user)
    await _append_round(db_session, session_id, [("call-1", _pending_content(proposal_id))])
    await db_session.commit()

    await deliver_device_control_conclusion(
        async_sessionmaker(db_engine, expire_on_commit=False), proposal_id
    )

    content = await _latest_assistant_text(db_engine, session_id)
    assert "被跳过" not in content
