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
    max_messages: int = 40,
) -> list[ChatMessage]:
    """Return this session's most recent messages as model-ready ChatMessages."""
    rows = await agent_message_crud.list_for_session(db, session_id, limit=max_messages)
    history: list[ChatMessage] = []
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


async def append_user_message(db: AsyncSession, session_id: int, content: str) -> AgentMessage:
    """Append one user turn."""
    return await agent_message_crud.append(db, session_id=session_id, role="user", content=content)


async def append_assistant_message(
    db: AsyncSession,
    session_id: int,
    content: str,
    *,
    tool_calls: list[ToolCall] | None = None,
) -> AgentMessage:
    """Append one assistant turn, optionally carrying the tool calls it requested."""
    serialized = None
    if tool_calls:
        serialized = [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls]
    return await agent_message_crud.append(
        db, session_id=session_id, role="assistant", content=content, tool_calls=serialized
    )


async def append_tool_result(
    db: AsyncSession,
    session_id: int,
    tool_call_id: str,
    content: str,
) -> AgentMessage:
    """Append one tool-result turn, correlated back to the call it answers."""
    return await agent_message_crud.append(
        db, session_id=session_id, role="tool", content=content, tool_call_id=tool_call_id
    )
