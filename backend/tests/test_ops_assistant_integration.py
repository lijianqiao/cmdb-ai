"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_ops_assistant_integration.py
@DateTime: 2026-08-12 13:20
@Docs: T11 运维助手跨层验收：会话 REST → mock chat_fn turn → 消息落库与 WS 事件。

实现流程：
1. 经 REST 创建会话，再把 FakeWebSocket 注册到进程内 hub（模拟前端已连上）。
2. patch chat_turn.chat（禁止真实 LLM），POST messages 触发整轮编排。
3. 断言 HTTP 返回 turn 摘要、GET messages 可见 user/assistant，且 WS 收到
   assistant_delta → turn_done。这覆盖「会话 + turn + 实时通道」闭环，不打真模型。
"""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.agent.ws_hub import hub
from app.core.llm import ChatMessage, ChatResult

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


@pytest_asyncio.fixture(autouse=True)
async def _clear_ws_hub() -> AsyncIterator[None]:
    """每个用例前后清空进程内 Hub，避免跨测串扰。"""
    hub._connections.clear()
    yield
    hub._connections.clear()


class FakeWebSocket:
    """记录 send_json 调用的假 WebSocket。"""

    def __init__(self) -> None:
        """初始化空发送记录。"""
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        """保存广播快照。"""
        self.sent.append(dict(data))


async def test_ops_assistant_session_turn_persists_and_broadcasts(
    client: AsyncClient,
    auth_headers: Headers,
) -> None:
    """创建会话 → mock LLM → 发消息：消息落库且 WS 收到 delta/turn_done。"""
    create_resp = await client.post(
        "/api/v1/agent/sessions",
        json={"title": "T11 集成验收"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["data"]["id"]

    ws = FakeWebSocket()
    await hub.connect(session_id, ws)  # type: ignore[arg-type]

    async def fake_chat(
        model_key: str,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            content="集成测试助手回复",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=5,
            completion_tokens=3,
        )

    with patch("app.agent.chat_turn.chat", new=fake_chat):
        post_resp = await client.post(
            f"/api/v1/agent/sessions/{session_id}/messages",
            json={"content": "你好，运维助手"},
            headers=auth_headers,
        )

    assert post_resp.status_code == 200, post_resp.text
    turn = post_resp.json()["data"]
    assert turn["reason"] == "final_answer"
    assert turn["final_answer"] == "集成测试助手回复"

    hist_resp = await client.get(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=auth_headers,
    )
    assert hist_resp.status_code == 200, hist_resp.text
    messages = hist_resp.json()["data"]
    assert len(messages) >= 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "你好，运维助手"
    assert any(
        m["role"] == "assistant" and m["content"] == "集成测试助手回复" for m in messages
    )

    types = [item["type"] for item in ws.sent]
    assert "assistant_delta" in types
    assert types[-1] == "turn_done"
    assert ws.sent[-1]["payload"]["reason"] == "final_answer"

    await hub.disconnect(session_id, ws)  # type: ignore[arg-type]
