"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_llm.py
@DateTime: 2026-08-13 13:15
@Docs: 统一 LLM 客户端单测：含数据库覆盖与环境回退。
"""

import json
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.data_encryption import encrypt_secret
from app.core.llm import MODELS, ChatMessage, LlmRequestError, ModelConfig, ToolCall, chat, embed
from app.crud.system_config import system_config_crud

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


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_chat_wraps_transport_failures_as_model_errors(
    error_type: type[httpx.RequestError],
) -> None:
    def fail_transport(request: httpx.Request) -> httpx.Response:
        raise error_type("transport secret", request=request)

    transport = httpx.MockTransport(fail_transport)
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        with pytest.raises(LlmRequestError) as raised:
            await chat(
                "local-chat",
                [ChatMessage(role="user", content="hi")],
                client=fake_client,
            )

    assert "transport secret" not in str(raised.value)
    assert isinstance(raised.value.__cause__, error_type)


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


async def test_embed_returns_vectors_in_index_order() -> None:
    transport = _fake_transport(
        {
            "data": [
                {"index": 1, "embedding": [0.4, 0.5]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ],
            "usage": {"prompt_tokens": 7},
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        result = await embed("local-embedding", ["第一段", "第二段"], client=fake_client)

    assert result.vectors == [[0.1, 0.2], [0.4, 0.5]]
    assert result.prompt_tokens == 7


async def test_embed_raises_on_non_200() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        with pytest.raises(LlmRequestError):
            await embed("local-embedding", ["x"], client=fake_client)


async def test_embed_rejects_unknown_model_key() -> None:
    with pytest.raises(LlmRequestError):
        await embed("does-not-exist", ["x"])


async def test_chat_reports_configured_token_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ModelConfig(
        name="priced-chat",
        capability="chat",
        base_url="http://model.test/v1",
        api_key="",
        request_model="priced-model",
        input_cost_per_million_usd=2.0,
        output_cost_per_million_usd=8.0,
    )
    monkeypatch.setitem(MODELS, "priced-chat", config)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1_000, "completion_tokens": 500},
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await chat(
            "priced-chat",
            [ChatMessage(role="user", content="hello")],
            client=client,
        )
    finally:
        await client.aclose()

    assert result.cost_usd == pytest.approx(0.006)


@pytest.mark.parametrize(
    ("model_key", "call"),
    [
        ("local-embedding", "chat"),
        ("local-chat", "embed"),
    ],
)
async def test_model_capability_mismatch_is_rejected_before_http_request(
    model_key: str, call: str
) -> None:
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP request must not be sent for an incompatible model")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fail_if_called), base_url="http://fake"
    ) as client:
        with pytest.raises(LlmRequestError, match="not registered"):
            if call == "chat":
                await chat(model_key, [ChatMessage(role="user", content="hi")], client=client)
            else:
                await embed(model_key, ["hi"], client=client)


def _sse_response(*events: dict[str, Any] | str) -> httpx.Response:
    """构造 OpenAI 兼容 SSE 响应（最后自动追加 data: [DONE]）。"""
    lines: list[str] = []
    for event in events:
        if isinstance(event, str):
            lines.append(f"data: {event}")
        else:
            lines.append(f"data: {json.dumps(event, ensure_ascii=False)}")
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return httpx.Response(
        200,
        content="\n".join(lines).encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )


async def test_chat_stream_invokes_on_delta_and_returns_aggregated_result() -> None:
    """stream=True 时按 token 回调 on_delta，最终仍返回完整 ChatResult。"""
    captured: dict[str, Any] = {}
    deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured["parsed"] = json.loads(request.content)
        return _sse_response(
            {
                "choices": [{"delta": {"content": "你"}, "index": 0}],
            },
            {
                "choices": [{"delta": {"content": "好"}, "index": 0}],
            },
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )

    async def on_delta(text: str) -> None:
        deltas.append(text)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        result = await chat(
            "local-chat",
            [ChatMessage(role="user", content="hi")],
            client=fake_client,
            stream=True,
            on_delta=on_delta,
        )

    assert captured["parsed"]["stream"] is True
    assert captured["parsed"]["stream_options"] == {"include_usage": True}
    assert deltas == ["你", "好"]
    assert result.content == "你好"
    assert result.tool_calls == []
    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 4
    assert result.completion_tokens == 2


async def test_chat_stream_accumulates_tool_call_deltas() -> None:
    """流式 tool_calls 按 index 拼装 arguments，不触发文本 on_delta。"""
    deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "kb_grep", "arguments": ""},
                                }
                            ]
                        },
                        "index": 0,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"pattern":'},
                                }
                            ]
                        },
                        "index": 0,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ' "重启"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                        "index": 0,
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 6},
            },
        )

    async def on_delta(text: str) -> None:
        deltas.append(text)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        result = await chat(
            "local-chat",
            [ChatMessage(role="user", content="查重启")],
            client=fake_client,
            stream=True,
            on_delta=on_delta,
        )

    assert deltas == []
    assert result.content is None
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [
        ToolCall(id="call_1", name="kb_grep", arguments='{"pattern": "重启"}')
    ]
    assert result.prompt_tokens == 8
    assert result.completion_tokens == 6


async def test_chat_stream_raises_on_non_200() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        with pytest.raises(LlmRequestError):
            await chat(
                "local-chat",
                [ChatMessage(role="user", content="hi")],
                client=fake_client,
                stream=True,
            )


async def test_chat_uses_database_model_config(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypted = encrypt_secret("db-chat-key")
    await system_config_crud.upsert_values(
        db_session,
        {
            "LLM_CHAT_BASE_URL": "https://db-chat.example/v1",
            "LLM_CHAT_API_KEY": encrypted,
            "LLM_CHAT_MODEL": "db-chat-model",
            "LLM_CHAT_INPUT_COST_PER_MILLION_USD": "2.0",
            "LLM_CHAT_OUTPUT_COST_PER_MILLION_USD": "4.0",
        },
        updated_by_user_id=None,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(
            "https://db-chat.example/v1/chat/completions"
        )
        assert request.headers["Authorization"] == "Bearer db-chat-key"
        assert json.loads(request.content)["model"] == "db-chat-model"
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {"content": "ok", "tool_calls": []},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    def fake_build_client(config: ModelConfig) -> httpx.AsyncClient:
        headers = (
            {"Authorization": f"Bearer {config.api_key}"}
            if config.api_key
            else {}
        )
        return httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            timeout=config.timeout_seconds,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("app.core.llm._build_client", fake_build_client)

    result = await chat(
        "local-chat",
        [ChatMessage(role="user", content="hi")],
        db=db_session,
    )
    assert result.cost_usd == pytest.approx(0.00004)


async def test_embed_uses_database_model_config(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypted = encrypt_secret("db-embed-key")
    await system_config_crud.upsert_values(
        db_session,
        {
            "LLM_EMBEDDING_BASE_URL": "https://db-embed.example/v1",
            "LLM_EMBEDDING_API_KEY": encrypted,
            "LLM_EMBEDDING_MODEL": "db-embed-model",
        },
        updated_by_user_id=None,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://db-embed.example/v1/embeddings")
        assert request.headers["Authorization"] == "Bearer db-embed-key"
        assert json.loads(request.content)["model"] == "db-embed-model"
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 3},
            },
        )

    def fake_build_client(config: ModelConfig) -> httpx.AsyncClient:
        headers = (
            {"Authorization": f"Bearer {config.api_key}"}
            if config.api_key
            else {}
        )
        return httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            timeout=config.timeout_seconds,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("app.core.llm._build_client", fake_build_client)

    result = await embed("local-embedding", ["测试文本"], db=db_session)
    assert result.vectors == [[0.1, 0.2]]
    assert result.prompt_tokens == 3


async def test_chat_without_db_uses_models_registry_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["model"] = json.loads(request.content)["model"]
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {"content": "ok", "tool_calls": []},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    def fake_build_client(config: ModelConfig) -> httpx.AsyncClient:
        captured["base_url"] = config.base_url
        captured["request_model"] = config.request_model
        return httpx.AsyncClient(
            base_url=config.base_url,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("app.core.llm._build_client", fake_build_client)

    await chat("local-chat", [ChatMessage(role="user", content="hi")])

    assert captured["base_url"] == settings.LLM_CHAT_BASE_URL
    assert captured["request_model"] == settings.LLM_CHAT_MODEL
    assert captured["url"].startswith(f"{settings.LLM_CHAT_BASE_URL}/chat/completions")
    assert captured["model"] == settings.LLM_CHAT_MODEL


async def test_injected_chat_client_works_without_db_kwarg() -> None:
    transport = _fake_transport(
        {
            "choices": [
                {"message": {"content": "mock", "tool_calls": []}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        result = await chat(
            "local-chat",
            [ChatMessage(role="user", content="hi")],
            client=fake_client,
        )
    assert result.content == "mock"
