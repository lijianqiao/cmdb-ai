"""Unified LLM client.

Every call into a model provider goes through `chat()`. New models are
registered by adding one entry to `MODELS` — nothing else in the codebase
should construct an HTTP client to a model provider directly.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """One entry in the model registry."""

    name: str
    base_url: str
    api_key: str
    request_model: str
    timeout_seconds: float = 60.0


MODELS: dict[str, ModelConfig] = {
    "local-chat": ModelConfig(
        name="local-chat",
        base_url=settings.LLM_BASE_URL,
        api_key=settings.llm_api_key,
        request_model=settings.LLM_CHAT_MODEL,
    ),
    "local-embedding": ModelConfig(
        name="local-embedding",
        base_url=settings.LLM_BASE_URL,
        api_key=settings.llm_api_key,
        request_model=settings.LLM_EMBEDDING_MODEL,
    ),
}


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call requested by the model."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One OpenAI-compatible chat message."""

    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None


@dataclass(frozen=True, slots=True)
class ChatResult:
    """The assistant turn returned by `chat()`."""

    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int


class LlmRequestError(RuntimeError):
    """Raised when the model provider returns a non-2xx response or a malformed body."""


def _message_to_payload(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in message.tool_calls
        ]
    return payload


def _build_client(config: ModelConfig) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    return httpx.AsyncClient(base_url=config.base_url, headers=headers, timeout=config.timeout_seconds)


async def chat(
    model_key: str,
    messages: list[ChatMessage],
    *,
    tools: list[dict[str, Any]] | None = None,
    client: httpx.AsyncClient | None = None,
) -> ChatResult:
    """Send one OpenAI-compatible chat completion request and return the assistant turn.

    `client` is injectable for tests (pass an `httpx.AsyncClient(transport=httpx.MockTransport(...))`);
    production callers omit it and a short-lived client is created per call.
    """
    config = MODELS.get(model_key)
    if config is None:
        raise LlmRequestError(f"unknown model key {model_key!r}; register it in MODELS first")

    payload: dict[str, Any] = {
        "model": config.request_model,
        "messages": [_message_to_payload(m) for m in messages],
    }
    if tools:
        payload["tools"] = tools

    owns_client = client is None
    http_client = client or _build_client(config)
    try:
        response = await http_client.post("/chat/completions", json=payload)
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code != 200:
        raise LlmRequestError(
            f"model {model_key!r} returned HTTP {response.status_code}: {response.text}"
        )

    body = response.json()
    try:
        choice = body["choices"][0]
        message = choice["message"]
        raw_tool_calls = message.get("tool_calls") or []
        tool_calls = [
            ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=tc["function"]["arguments"])
            for tc in raw_tool_calls
        ]
        usage = body.get("usage", {})
        return ChatResult(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice["finish_reason"],
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
    except (KeyError, IndexError) as exc:
        raise LlmRequestError(f"malformed response body from model {model_key!r}: {body}") from exc
