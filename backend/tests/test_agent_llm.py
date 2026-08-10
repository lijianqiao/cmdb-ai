"""Tests for the unified LLM client (app.core.llm)."""

import json
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.core.llm import MODELS, ChatMessage, LlmRequestError, ToolCall, chat

pytestmark = pytest.mark.asyncio


def _fake_transport(json_body: dict[str, object], status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body)

    return httpx.MockTransport(handler)


async def test_chat_returns_parsed_text_result() -> None:
    transport = _fake_transport(
        {
            "choices": [
                {"message": {"content": "你好", "tool_calls": []}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        result = await chat(
            "local-chat",
            [ChatMessage(role="user", content="在吗")],
            client=fake_client,
        )

    assert result.content == "你好"
    assert result.tool_calls == []
    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 3


async def test_chat_parses_tool_calls() -> None:
    transport = _fake_transport(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "kb_grep", "arguments": '{"pattern": "重启"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5},
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        result = await chat(
            "local-chat",
            [ChatMessage(role="user", content="帮我查一下重启流程")],
            client=fake_client,
        )

    assert result.content is None
    assert result.tool_calls == [ToolCall(id="call_1", name="kb_grep", arguments='{"pattern": "重启"}')]


async def test_chat_replays_tool_calls_and_tool_call_id_in_request_body() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["parsed"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "好的", "tool_calls": []}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        await chat(
            "local-chat",
            [
                ChatMessage(role="user", content="查一下"),
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id="call_1", name="kb_grep", arguments="{}")],
                ),
                ChatMessage(role="tool", content="没找到", tool_call_id="call_1"),
            ],
            client=fake_client,
        )

    sent_messages = captured["parsed"]["messages"]
    assert sent_messages[1]["tool_calls"][0]["function"]["name"] == "kb_grep"
    assert sent_messages[2]["tool_call_id"] == "call_1"


async def test_chat_raises_on_non_200() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        with pytest.raises(LlmRequestError):
            await chat("local-chat", [ChatMessage(role="user", content="hi")], client=fake_client)


async def test_chat_raises_llm_request_error_on_invalid_json_body() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="not json"))
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        with pytest.raises(LlmRequestError):
            await chat("local-chat", [ChatMessage(role="user", content="hi")], client=fake_client)


async def test_chat_rejects_unknown_model_key() -> None:
    with pytest.raises(LlmRequestError):
        await chat("does-not-exist", [ChatMessage(role="user", content="hi")])


async def test_chat_and_embedding_models_use_independent_connection_settings() -> None:
    """Chat and embedding must be individually pointable at different providers.

    conftest.py sets LLM_CHAT_BASE_URL/LLM_EMBEDDING_BASE_URL to distinct
    values specifically so this test fails if the two registry entries ever
    collapse back onto one shared base_url/api_key pair.
    """
    assert MODELS["local-chat"].base_url == settings.LLM_CHAT_BASE_URL
    assert MODELS["local-embedding"].base_url == settings.LLM_EMBEDDING_BASE_URL
    assert MODELS["local-chat"].base_url != MODELS["local-embedding"].base_url
