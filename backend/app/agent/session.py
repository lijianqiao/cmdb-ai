"""Transcript helpers built on top of the AgentMessage CRUD layer.

`build_model_history` assembles the model window: code-injected system prompt,
optional root-session LLM summary (never for child agents), then a bounded recent
raw transcript. Full audit history stays in `agent_messages` unchanged.

The window covers committed rows plus the current loop's in-memory messages
(``pending``): a running loop keeps its new assistant/tool messages in memory and
writes them once at the end (``persist_transcript``), so it holds no database
connection while it waits for the model or tools (R5).
"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.compaction import (
    COMPACT_FALLBACK_MAX_MESSAGES,
    COMPACT_RECENT_RAW_MESSAGES,
    MEMORY_SUMMARY_USER_PREFIX,
    TOOL_RESULT_UNTRUSTED_PREFIX,
)
from app.agent.transcript import TranscriptMessage
from app.core.database import SessionSource, session_scope
from app.core.llm import ChatMessage, ToolCall
from app.crud.agent_message import agent_message_crud
from app.models.agent_message import AgentMessage
from app.models.agent_session import AgentSession


def _to_chat_messages(messages: Sequence[TranscriptMessage]) -> list[ChatMessage]:
    chat_messages: list[ChatMessage] = []
    for message in messages:
        tool_calls: list[ToolCall] | None = None
        if message.tool_calls:
            tool_calls = [
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                for tc in message.tool_calls
            ]
        content = (
            TOOL_RESULT_UNTRUSTED_PREFIX + message.content
            if message.role == "tool"
            else message.content
        )
        chat_messages.append(
            ChatMessage(
                role=message.role,
                content=content,
                tool_call_id=message.tool_call_id,
                tool_calls=tool_calls,
            )
        )
    return chat_messages


async def build_model_history(
    db: SessionSource,
    session_id: int,
    *,
    agent_id: str | None = None,
    system_prompt: str | None = None,
    max_messages: int = COMPACT_FALLBACK_MAX_MESSAGES,
    pending: Sequence[TranscriptMessage] = (),
) -> list[ChatMessage]:
    """Return one exact Agent's bounded history with its code-owned instructions.

    Committed rows are read in one short session and turned into plain data before
    it closes; ``pending`` messages (this loop's, not yet written) follow them and
    share the same window, exactly as if they had already been committed.
    """
    history: list[ChatMessage] = []
    if system_prompt is not None:
        history.append(ChatMessage(role="system", content=system_prompt))

    summary: str | None = None
    window = max_messages
    async with session_scope(db) as session:
        if agent_id is None:
            agent_session = await session.get(AgentSession, session_id)
            if agent_session is not None and agent_session.memory_summary:
                summary = agent_session.memory_summary
                window = COMPACT_RECENT_RAW_MESSAGES
                rows = await agent_message_crud.list_for_agent_after_id(
                    session,
                    session_id,
                    agent_id=None,
                    after_id=agent_session.compacted_through_message_id,
                    limit=window,
                )
            else:
                rows = await agent_message_crud.list_for_agent(
                    session, session_id, agent_id=None, limit=window
                )
        else:
            rows = await agent_message_crud.list_for_agent(
                session, session_id, agent_id=agent_id, limit=window
            )
        committed = [TranscriptMessage.from_row(row) for row in rows]

    if summary is not None:
        history.append(
            ChatMessage(role="user", content=f"{MEMORY_SUMMARY_USER_PREFIX}\n{summary}")
        )

    # 本轮消息都比已提交的行新：取两者拼接后的最后 window 条，与它们都已写库时的窗口一致
    messages = [*committed, *pending][-window:]
    # 窗口截断可能把开头的 tool 结果与它的 assistant(tool_calls) 消息切开；
    # 孤立的 tool 消息对 OpenAI 兼容端点是非法历史，直接丢弃到合法边界。
    # 正常按完整工具单元压缩后，recent 窗口不应触发此分支；此处保留给 fallback 窗口与旧数据。
    start = 0
    while start < len(messages) and messages[start].role == "tool":
        start += 1
    history.extend(_to_chat_messages(messages[start:]))
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


async def append_transcript(
    db: AsyncSession,
    session_id: int,
    messages: Sequence[TranscriptMessage],
    *,
    agent_id: str | None = None,
) -> list[int]:
    """Append this loop's in-memory messages in order; return their new primary keys."""
    ids: list[int] = []
    for message in messages:
        row = await agent_message_crud.append(
            db,
            session_id=session_id,
            agent_id=agent_id,
            role=message.role,
            content=message.content,
            tool_call_id=message.tool_call_id,
            tool_calls=message.tool_calls,
        )
        ids.append(row.id)
    return ids


async def persist_transcript(
    db: SessionSource,
    session_id: int,
    messages: Sequence[TranscriptMessage],
    usage_index: int | None,
    *,
    agent_id: str | None = None,
) -> int | None:
    """Write one loop's messages in a single short transaction.

    Returns the primary key of ``messages[usage_index]`` (the reply that carries the
    turn's usage), or None when there is nothing to write or no such message.
    """
    if not messages:
        return None
    async with session_scope(db, commit=True) as session:
        ids = await append_transcript(session, session_id, messages, agent_id=agent_id)
    return ids[usage_index] if usage_index is not None else None
