"""turn 租约超时接管、中文 token 估算、压缩预算记账的回归测试。

三条都是「不影响功能、只在长期运行后显形」的问题，功能测试抓不到：
- 租约没有超时 → 进程存活但 turn 任务消失时，会话被永久 409 锁死；
- token 按字符估算 → 中文低估 4 倍，长会话每轮第一次调用可能直接超窗；
- 压缩超预算时不记账 → 每步空烧一次摘要调用，账面上看不见。
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import Budget
from app.agent.compaction import _estimate_text_tokens, ensure_root_compaction
from app.agent.session import append_assistant_message, append_user_message
from app.core.config import settings
from app.core.llm import ChatResult
from app.crud.agent_session import agent_session_crud
from app.models.agent_session import AgentSession
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_session(db_session: AsyncSession, user_id: int) -> int:
    session = await agent_session_crud.create(
        db_session, {"user_id": user_id, "title": "", "status": "active"}
    )
    await db_session.flush()
    return session.id


async def test_claim_turn_rejects_a_live_lease(
    db_session: AsyncSession, test_user: User
) -> None:
    """未超时的活跃租约不可被抢占——这是 turn 串行化的基本保证。"""
    session_id = await _make_session(db_session, test_user.id)

    assert await agent_session_crud.claim_turn(db_session, session_id, "token-a") is True
    assert await agent_session_crud.claim_turn(db_session, session_id, "token-b") is False


async def test_claim_turn_takes_over_a_stale_lease(
    db_session: AsyncSession, test_user: User
) -> None:
    """超时的陈旧租约可被接管。

    没有这条，进程存活但 turn 任务已消失时会话会被**永久**锁死（对任何新消息
    返回 409），只有重启触发 recover_active_turns 才能恢复。
    """
    session_id = await _make_session(db_session, test_user.id)
    assert await agent_session_crud.claim_turn(db_session, session_id, "stuck") is True

    # 把租约时间推回到超时阈值之前，模拟一个卡死很久的 turn
    session = await db_session.get(AgentSession, session_id)
    assert session is not None
    session.active_turn_started_at = datetime.now(UTC) - timedelta(
        seconds=settings.AGENT_TURN_LEASE_TIMEOUT_SECONDS + 60
    )
    await db_session.flush()

    assert await agent_session_crud.claim_turn(db_session, session_id, "fresh") is True

    refreshed = await db_session.get(AgentSession, session_id)
    assert refreshed is not None
    assert refreshed.active_turn_token == "fresh"


async def test_stale_takeover_threshold_exceeds_worst_case_turn() -> None:
    """接管阈值必须大于单轮最坏耗时。

    否则会抢占一个还在正常执行的 turn，造成两个 turn 并发写同一份 transcript
    ——那比卡住更糟。这条断言把这个约束钉死，改任何一个相关配置都会被提醒。
    """
    from app.core.llm import MODELS

    # 取三档里最慢的那个：一轮对话可能落到任意一档，最坏耗时按最慢的算
    slowest_chat_timeout = max(
        config.timeout_seconds
        for config in MODELS.values()
        if config.capability == "chat"
    )
    worst_case = Budget().max_steps * (
        slowest_chat_timeout
        + settings.DEVICE_COMMAND_CONN_TIMEOUT_SECONDS
        + settings.DEVICE_COMMAND_READ_TIMEOUT_SECONDS
    )
    assert settings.AGENT_TURN_LEASE_TIMEOUT_SECONDS > worst_case, (
        f"租约超时 {settings.AGENT_TURN_LEASE_TIMEOUT_SECONDS}s 不大于单轮最坏耗时 "
        f"{worst_case}s，会抢占正常运行中的 turn"
    )


async def test_release_turn_only_by_holder(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await agent_session_crud.claim_turn(db_session, session_id, "mine")

    assert await agent_session_crud.release_turn(db_session, session_id, "other") is False
    assert await agent_session_crud.release_turn(db_session, session_id, "mine") is True


@pytest.mark.asyncio(loop_scope="function")
async def test_token_estimate_does_not_underestimate_chinese() -> None:
    """中文 token 估算不得大幅低估。

    原实现 len(text) // 4 对英文成立、对中文低估 4 倍。这是一个中文运维助手，
    低估会让长会话每轮的第一次调用带着远超预期的上下文发出去（第一次调用只能
    靠估算，run_loop 的 last_prompt_tokens 每轮从 None 开始）。
    """
    chinese = "核心交换机重启后接口状态异常" * 100
    estimate = _estimate_text_tokens(chinese)

    # 中文实际约 1 token/字符；估算不得低于真实值的一半
    assert estimate >= len(chinese) * 0.5, (
        f"{len(chinese)} 个中文字符估算成 {estimate} tokens，低估过多"
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_token_estimate_stays_reasonable_for_english() -> None:
    """英文不能因为修中文而被高估太多，否则会过早触发压缩浪费预算。"""
    english = "core switch interface status abnormal after reboot " * 100
    estimate = _estimate_text_tokens(english)

    # 英文约 4 字符/token；允许在 0.5~2 倍区间
    expected = len(english) / 4
    assert expected * 0.5 <= estimate <= expected * 2.0, (
        f"{len(english)} 个英文字符估算成 {estimate} tokens，偏离合理区间"
    )


async def test_compaction_records_cost_even_when_over_budget(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超预算时摘要被丢弃，但**成本必须计入**。

    原实现在 record_cost 之前先判断「加上会不会超」，超了就直接 return——
    于是成本不进预算、compacted_through_message_id 也不推进，下一步面对完全
    相同的输入再调一次。max_steps=20 时一轮最多空烧 20 次摘要调用而账面无感。
    """
    session_id = await _make_session(db_session, test_user.id)
    for i in range(25):
        await append_user_message(db_session, session_id, f"用户消息-{i}")
        await append_assistant_message(db_session, session_id, f"助手回复-{i}")
    await db_session.commit()

    async def expensive_chat(model_key, messages, **kwargs):
        return ChatResult(
            content="摘要正文",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=99.0,  # 远超预算
        )

    monkeypatch.setattr("app.agent.compaction.chat", expensive_chat)
    monkeypatch.setattr("app.agent.compaction.COMPACT_TOKEN_THRESHOLD", 1)

    budget = Budget()
    await ensure_root_compaction(
        db_session, session_id, budget=budget, system_prompt="根指令"
    )

    # 钱花了就要记账
    assert budget.cost_used_usd == pytest.approx(99.0)
    # 超预算时不采纳摘要
    stored = await db_session.get(AgentSession, session_id)
    assert stored is not None
    assert stored.memory_summary is None


async def test_compaction_adopts_summary_within_budget(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预算充足时正常采纳摘要并推进游标——确认上面的改动没有误伤正常路径。"""
    session_id = await _make_session(db_session, test_user.id)
    for i in range(25):
        await append_user_message(db_session, session_id, f"用户消息-{i}")
        await append_assistant_message(db_session, session_id, f"助手回复-{i}")
    await db_session.commit()

    async def cheap_chat(model_key, messages, **kwargs):
        return ChatResult(
            content="便宜的摘要",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.001,
        )

    monkeypatch.setattr("app.agent.compaction.chat", cheap_chat)
    monkeypatch.setattr("app.agent.compaction.COMPACT_TOKEN_THRESHOLD", 1)

    budget = Budget()
    await ensure_root_compaction(
        db_session, session_id, budget=budget, system_prompt="根指令"
    )

    assert budget.cost_used_usd == pytest.approx(0.001)
    stored = await db_session.get(AgentSession, session_id)
    assert stored is not None
    assert stored.memory_summary == "便宜的摘要"
    assert stored.compacted_through_message_id is not None
