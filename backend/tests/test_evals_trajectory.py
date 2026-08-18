"""从 agent_message / hitl_proposal 读回一轮轨迹。

实现流程：
1. eval 要打三层分（结果 / 轨迹不变量 / 效率），原料全在数据库里现成：
   agent_message 存了 tool_calls、prompt_tokens、completion_tokens、cost_usd，
   hitl_proposal 存了危险动作有没有走审批。**所以 eval 不需要新建任何埋点。**
2. `after_message_id` 划定本轮边界：一个会话会跑很多轮，打分只能看这一轮，
   否则上一轮调过的工具会被算进来，不变量检查就会误判。
3. 这个模块只读不写，用 conftest 的 SQLite fixture 就能完整单测，一分钱不花。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_message import AgentMessage
from app.models.agent_session import AgentSession
from app.models.hitl_proposal import HitlProposal
from app.models.user import User
from evals.trajectory import load_trajectory


async def _new_session(db_session: AsyncSession, user: User) -> int:
    agent_session = AgentSession(user_id=user.id, title="eval")
    db_session.add(agent_session)
    await db_session.flush()
    return agent_session.id


async def _boundary(db_session: AsyncSession, session_id: int, text: str) -> int:
    message = AgentMessage(session_id=session_id, role="user", content=text)
    db_session.add(message)
    await db_session.flush()
    return message.id


async def test_collects_tool_names_in_call_order(
    db_session: AsyncSession, superuser: User
) -> None:
    """工具调用顺序按消息主键排，轨迹不变量检查全靠它。"""
    session_id = await _new_session(db_session, superuser)
    start = await _boundary(db_session, session_id, "问题")

    db_session.add(
        AgentMessage(
            session_id=session_id,
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "name": "kb_grep", "arguments": "{}"}],
        )
    )
    await db_session.flush()
    db_session.add(
        AgentMessage(
            session_id=session_id,
            role="assistant",
            content="",
            tool_calls=[{"id": "c2", "name": "kb_read", "arguments": "{}"}],
        )
    )
    await db_session.flush()
    db_session.add(
        AgentMessage(
            session_id=session_id,
            role="assistant",
            content="最终答案",
            prompt_tokens=100,
            completion_tokens=20,
            cost_usd=0.001,
        )
    )
    await db_session.flush()

    trajectory = await load_trajectory(
        db_session, session_id=session_id, after_message_id=start
    )

    assert trajectory.tool_names == ("kb_grep", "kb_read")
    assert trajectory.final_answer == "最终答案"
    assert trajectory.steps == 3
    assert trajectory.prompt_tokens == 100
    assert trajectory.completion_tokens == 20
    assert trajectory.cost_usd == 0.001


async def test_ignores_messages_from_earlier_turns(
    db_session: AsyncSession, superuser: User
) -> None:
    """上一轮调过的工具不能算进这一轮，否则 must_not_call 会误报。"""
    session_id = await _new_session(db_session, superuser)
    db_session.add(
        AgentMessage(
            session_id=session_id,
            role="assistant",
            content="",
            tool_calls=[{"id": "old", "name": "device_control", "arguments": "{}"}],
        )
    )
    await db_session.flush()
    boundary = await _boundary(db_session, session_id, "新问题")
    db_session.add(AgentMessage(session_id=session_id, role="assistant", content="答案"))
    await db_session.flush()

    trajectory = await load_trajectory(
        db_session, session_id=session_id, after_message_id=boundary
    )

    assert "device_control" not in trajectory.tool_names
    assert trajectory.steps == 1


async def test_ignores_other_sessions(
    db_session: AsyncSession, superuser: User
) -> None:
    """eval 每次运行开新会话，串了会话就等于把别的用例的工具算进来。"""
    mine = await _new_session(db_session, superuser)
    other = await _new_session(db_session, superuser)
    start = await _boundary(db_session, mine, "问题")

    db_session.add(
        AgentMessage(
            session_id=other,
            role="assistant",
            content="",
            tool_calls=[{"id": "x", "name": "device_control", "arguments": "{}"}],
        )
    )
    db_session.add(AgentMessage(session_id=mine, role="assistant", content="答案"))
    await db_session.flush()

    trajectory = await load_trajectory(
        db_session, session_id=mine, after_message_id=start
    )

    assert trajectory.tool_names == ()


async def test_collects_hitl_proposal_statuses(
    db_session: AsyncSession, superuser: User
) -> None:
    """安全类用例要断言「提案建了、但没执行」，状态必须读得到。"""
    session_id = await _new_session(db_session, superuser)
    start = await _boundary(db_session, session_id, "清空配置")
    db_session.add(
        HitlProposal(
            session_id=session_id,
            action_type="device_control",
            action_payload={"command": "reset saved-configuration"},
        )
    )
    await db_session.flush()

    trajectory = await load_trajectory(
        db_session, session_id=session_id, after_message_id=start
    )

    assert trajectory.proposal_statuses == ("PENDING",)


async def test_final_answer_is_the_last_non_empty_assistant_message(
    db_session: AsyncSession, superuser: User
) -> None:
    """带 tool_calls 的 assistant 消息 content 是空的，不能把它当成最终答案。"""
    session_id = await _new_session(db_session, superuser)
    start = await _boundary(db_session, session_id, "问题")

    db_session.add(
        AgentMessage(session_id=session_id, role="assistant", content="中间想法")
    )
    await db_session.flush()
    db_session.add(
        AgentMessage(
            session_id=session_id,
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "name": "kb_grep", "arguments": "{}"}],
        )
    )
    await db_session.flush()
    db_session.add(
        AgentMessage(session_id=session_id, role="assistant", content="真正的答案")
    )
    await db_session.flush()

    trajectory = await load_trajectory(
        db_session, session_id=session_id, after_message_id=start
    )

    assert trajectory.final_answer == "真正的答案"


async def test_empty_turn_yields_zeroed_trajectory(
    db_session: AsyncSession, superuser: User
) -> None:
    """模型一句话没说也要返回结构完整的轨迹，不能抛异常——打分器要能判它 FAIL。"""
    session_id = await _new_session(db_session, superuser)
    start = await _boundary(db_session, session_id, "问题")

    trajectory = await load_trajectory(
        db_session, session_id=session_id, after_message_id=start
    )

    assert trajectory.final_answer == ""
    assert trajectory.tool_names == ()
    assert trajectory.steps == 0
    assert trajectory.cost_usd == 0.0
