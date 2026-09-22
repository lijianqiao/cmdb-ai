"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: transcript.py
@DateTime: 2026-09-22
@Docs: 对话消息的纯数据形状：已写库的行与本轮还在内存里的消息共用（R5）。

实现流程：
1. 根 turn 与子 Agent 在等模型、等工具时不占数据库连接：本轮新产生的 assistant/tool
   消息先留在内存，整轮结束才一次写库（出错或被取消就直接丢弃，与原来整轮回滚一致）。
2. 组装模型历史、估算压缩窗口时，要把「已提交的行」和「内存里的消息」拼成一个列表处理，
   所以两者统一成 TranscriptMessage；id 为 None 表示本轮产生、还没写库。
3. 数据库行在短会话里就转成这个形状，离开会话后不再持有 ORM 对象——ORM 对象在会话
   关闭后访问未加载的属性会触发延迟查询，那正是要避免的「等模型时又去碰数据库」。
"""

from dataclasses import dataclass
from typing import Self

from app.core.llm import ToolCall
from app.models.agent_message import AgentMessage


@dataclass(frozen=True, slots=True)
class TranscriptMessage:
    """一条对话消息；id 为 None 表示本轮产生、尚未写库。"""

    role: str
    content: str
    tool_calls: list[dict[str, str]] | None = None
    tool_call_id: str | None = None
    id: int | None = None

    @classmethod
    def from_row(cls, row: AgentMessage) -> Self:
        """把已写库的行转成纯数据。"""
        return cls(
            role=row.role,
            content=row.content,
            tool_calls=row.tool_calls,
            tool_call_id=row.tool_call_id,
            id=row.id,
        )

    @classmethod
    def assistant(cls, content: str, tool_calls: list[ToolCall] | None = None) -> Self:
        """本轮模型回复；带工具调用时按写库的 JSON 形状保存。"""
        serialized = (
            [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in tool_calls]
            if tool_calls
            else None
        )
        return cls(role="assistant", content=content, tool_calls=serialized)

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> Self:
        """本轮一次工具调用的结果。"""
        return cls(role="tool", content=content, tool_call_id=tool_call_id)
