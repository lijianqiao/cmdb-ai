"""Transcript helpers built on top of the AgentMessage CRUD layer.

Deliberately no compaction/summarization here yet — `build_model_history`
returns a bounded recent window only. Summarizing older turns is deferred to
a later plan (see docs/AGENT_ARCHITECTURE.md §8 and guide.md §6.3); adding it
now would be speculative for a subsystem nothing yet exercises end-to-end.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import ChatMessage, ToolCall
from app.crud.agent_message import agent_message_crud
from app.models.agent_message import AgentMessage


async def build_model_history(
    db: AsyncSession,
    session_id: int,
    *,
    agent_id: str | None = None,
    system_prompt: str | None = None,
    max_messages: int = 40,
) -> list[ChatMessage]:
    """Return one exact Agent's bounded history with its code-owned instructions."""
    rows = await agent_message_crud.list_for_agent(
        db, session_id, agent_id=agent_id, limit=max_messages
    )
    history: list[ChatMessage] = []
    if system_prompt is not None:
        history.append(ChatMessage(role="system", content=system_prompt))
    for row in rows:
        tool_calls: list[ToolCall] | None = None
        if row.tool_calls:
            tool_calls = [
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                for tc in row.tool_calls
            ]
        history.append(
            ChatMessage(
                role=row.role,
                content=row.content,
                tool_call_id=row.tool_call_id,
                tool_calls=tool_calls,
            )
        )
    return history


async def append_user_message(
    db: AsyncSession,
    session_id: int,
    content: str,
    *,
    agent_id: str | None = None,
) -> AgentMessage:
    """Append one user/root-or-parent input to one exact Agent transcript."""
    return await agent_message_crud.append(
        db,
        session_id=session_id,
        agent_id=agent_id,
        role="user",
        content=content,
    )


async def append_assistant_message(
    db: AsyncSession,
    session_id: int,
    content: str,
    *,
    agent_id: str | None = None,
    tool_calls: list[ToolCall] | None = None,
) -> AgentMessage:
    """Append one assistant turn to one exact Agent transcript."""
    serialized = None
    if tool_calls:
        serialized = [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls]
    return await agent_message_crud.append(
        db,
        session_id=session_id,
        agent_id=agent_id,
        role="assistant",
        content=content,
        tool_calls=serialized,
    )


async def append_tool_result(
    db: AsyncSession,
    session_id: int,
    tool_call_id: str,
    content: str,
    *,
    agent_id: str | None = None,
) -> AgentMessage:
    """Append one correlated tool result to one exact Agent transcript."""
    return await agent_message_crud.append(
        db,
        session_id=session_id,
        agent_id=agent_id,
        role="tool",
        content=content,
        tool_call_id=tool_call_id,
    )
