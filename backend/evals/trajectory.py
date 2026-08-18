"""把一轮 Agent turn 的轨迹从数据库读回来。

实现流程：
1. eval 打三层分要的原料——调了哪些工具、走了几步、花了多少 token 和钱、
   危险动作有没有走审批——数据库里全都现成：agent_message 存 tool_calls /
   prompt_tokens / completion_tokens / cost_usd，hitl_proposal 存提案状态。
   **所以 eval 不需要在 Agent 里新建任何埋点。**
2. `after_message_id` 划定本轮边界：一个会话会跑很多轮，打分只能看这一轮，
   否则上一轮调过的工具会被算进来，`must_not_call` 这类不变量就会误报。
3. 这个模块只读不写，所以能用 SQLite fixture 完整单测，零成本零随机——
   eval 自身的 bug 不该等到花钱跑真模型时才发现。
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_message import AgentMessage
from app.models.hitl_proposal import HitlProposal


@dataclass(frozen=True, slots=True)
class Trajectory:
    """一轮 turn 的全部可观测事实，三层打分的唯一输入。"""

    final_answer: str
    tool_names: tuple[str, ...]
    steps: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    proposal_statuses: tuple[str, ...]


async def load_trajectory(
    session: AsyncSession, *, session_id: int, after_message_id: int
) -> Trajectory:
    """读回 `session_id` 在 `after_message_id` 之后产生的这一轮轨迹。

    Args:
        session: 数据库会话
        session_id: Agent 会话 ID
        after_message_id: 本轮起点（用户提问那条消息的主键），只统计它之后的行

    Returns:
        本轮轨迹。这一轮什么都没产生时返回全零值而不是抛异常——
        打分器要能把「模型一句话没说」判成 FAIL，而不是自己先崩掉。
    """
    rows = (
        (
            await session.execute(
                select(AgentMessage)
                .where(
                    AgentMessage.session_id == session_id,
                    AgentMessage.id > after_message_id,
                )
                .order_by(AgentMessage.id)
            )
        )
        .scalars()
        .all()
    )

    tool_names: list[str] = []
    final_answer = ""
    steps = 0
    prompt_tokens = 0
    completion_tokens = 0
    cost_usd = 0.0

    for row in rows:
        if row.role == "assistant":
            steps += 1
            # 带 tool_calls 的那条 content 是空的，不能覆盖掉真正的答案
            if row.content:
                final_answer = row.content
        for call in row.tool_calls or []:
            name = call.get("name")
            if name:
                tool_names.append(name)
        prompt_tokens += row.prompt_tokens or 0
        completion_tokens += row.completion_tokens or 0
        cost_usd += row.cost_usd or 0.0

    # 提案按会话查而不是按消息 id：eval 每次运行都开新会话，
    # 这一个会话里出现的提案必然属于这一轮。
    proposals = (
        (
            await session.execute(
                select(HitlProposal.status)
                .where(HitlProposal.session_id == session_id)
                .order_by(HitlProposal.id)
            )
        )
        .scalars()
        .all()
    )

    return Trajectory(
        final_answer=final_answer,
        tool_names=tuple(tool_names),
        steps=steps,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        proposal_statuses=tuple(proposals),
    )
