"""Unified LLM client.

Every call into a model provider goes through `chat()`. New models are
registered by adding one entry to `MODELS` — nothing else in the codebase
should construct an HTTP client to a model provider directly.

`chat(..., stream=False)` 保持整段返回；`stream=True` 时走 OpenAI 兼容 SSE，
可选 `on_delta` 回调每段文本增量，结束后仍返回完整 `ChatResult`。
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.data_encryption import DataDecryptError, DataEncryptionKeyMissingError
from app.services.system_config import get_effective_llm_config

logger = logging.getLogger(__name__)

type ChatDeltaCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """One typed model-registry entry."""

    name: str
    capability: Literal["chat", "embedding"]
    base_url: str
    api_key: str
    request_model: str
    timeout_seconds: float = 60.0
    input_cost_per_million_usd: float = 0.0
    output_cost_per_million_usd: float = 0.0


MODELS: dict[str, ModelConfig] = {
    "local-chat": ModelConfig(
        name="local-chat",
        capability="chat",
        base_url=settings.LLM_CHAT_BASE_URL,
        api_key=settings.llm_chat_api_key,
        request_model=settings.LLM_CHAT_MODEL,
        input_cost_per_million_usd=settings.LLM_CHAT_INPUT_COST_PER_MILLION_USD,
        output_cost_per_million_usd=settings.LLM_CHAT_OUTPUT_COST_PER_MILLION_USD,
    ),
    "local-embedding": ModelConfig(
        name="local-embedding",
        capability="embedding",
        base_url=settings.LLM_EMBEDDING_BASE_URL,
        api_key=settings.llm_embedding_api_key,
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
    cost_usd: float = 0.0


class LlmRequestError(RuntimeError):
    """Raised when the model provider returns a non-2xx response or a malformed body."""


def _error_result(reason: str) -> ChatResult:
    """构造 finish_reason=error 的 ChatResult，供传输/HTTP/解析失败共用。"""
    return ChatResult(
        content=f"模型调用失败：{reason}",
        tool_calls=[],
        finish_reason="error",
        prompt_tokens=0,
        completion_tokens=0,
        cost_usd=0.0,
    )


_SENSITIVE_AUTH_RE = re.compile(
    r"authorization\s*:\s*(?:bearer\s+\S+|\S+)|bearer\s+\S+",
    re.IGNORECASE,
)


def _redact_sensitive_auth(text: str) -> str:
    """脱敏 HTTP 错误正文中的 Authorization / Bearer 凭证片段。"""
    return _SENSITIVE_AUTH_RE.sub("[已脱敏]", text)


def _http_error_reason(status_code: int, body: str) -> str:
    """HTTP 非 200 时的中文短因；正文最多截断 200 字符并脱敏凭证。"""
    truncated = body[:200] if body else ""
    if truncated:
        sanitized = _redact_sensitive_auth(truncated)
        return f"HTTP {status_code}：{sanitized}"
    return f"HTTP {status_code}"


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


# 按 (事件循环, 连接身份) 复用客户端，避免每次调用重做 TCP+TLS 握手。
# 一轮 Agent 对话最多 max_steps 次模型调用，逐次新建客户端等于逐次完整握手。
# - 连接身份进键：LLM 配置可在系统设置里热改（见 _resolve_model_config），
#   改完要自然切到新客户端，不能只按 model_key 缓存。
# - 事件循环进键：AsyncClient 的连接池持有绑定到具体 loop 的 socket，而 pytest
#   配的是每用例一个新 loop，跨 loop 复用会抛 "Event loop is closed"。
#   直接持 loop 对象而不是 id()，避免 loop 回收后 id 复用导致误命中。
_CLIENT_CACHE: dict[tuple[asyncio.AbstractEventLoop, str, str, float], httpx.AsyncClient] = {}


def _build_client(config: ModelConfig) -> httpx.AsyncClient:
    """构造一个新的客户端。

    与 _shared_client 分开是刻意的：这里只管「怎么造」，缓存策略在外层。
    单测通过 monkeypatch 这个函数注入 MockTransport，接缝必须保留。
    """
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    return httpx.AsyncClient(
        base_url=config.base_url,
        headers=headers,
        timeout=config.timeout_seconds,
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    )


def _shared_client(config: ModelConfig) -> httpx.AsyncClient:
    """返回按连接身份与事件循环复用的客户端。"""
    key = (
        asyncio.get_running_loop(),
        config.base_url,
        config.api_key,
        config.timeout_seconds,
    )
    cached = _CLIENT_CACHE.get(key)
    if cached is not None and not cached.is_closed:
        return cached
    created = _build_client(config)
    _CLIENT_CACHE[key] = created
    return created


async def close_llm_clients() -> None:
    """释放**当前事件循环**上的缓存客户端；由 app.main 的 lifespan 在关停时调用。

    只关本 loop 的：AsyncClient 的连接池绑定 loop，去 aclose 别的 loop 上的客户端
    （测试进程里每个用例一个 loop，缓存里会留下已结束 loop 的条目）会在关停路径上
    抛 CancelledError，进而把后面的 engine.dispose() 一起带崩。

    单个客户端关闭失败也不能中断关停流程——此时进程本来就要退出了。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for key in [cached_key for cached_key in _CLIENT_CACHE if cached_key[0] is loop]:
        client = _CLIENT_CACHE.pop(key, None)
        if client is None:
            continue
        try:
            await client.aclose()
        except Exception:
            logger.warning("关闭 LLM 客户端失败 base_url=%s", key[1], exc_info=True)


def _cost_usd(config: ModelConfig, prompt_tokens: int, completion_tokens: int) -> float:
    return (
        prompt_tokens * config.input_cost_per_million_usd
        + completion_tokens * config.output_cost_per_million_usd
    ) / 1_000_000


async def _resolve_model_config(
    model_key: str,
    db: AsyncSession | None,
) -> ModelConfig:
    """
    按模型键解析本次请求应使用的 ModelConfig。

    有 db 时读取数据库有效配置覆盖 local-chat / local-embedding；
    无 db 或未知键时回退 MODELS 登记表。
    """
    base = MODELS.get(model_key)
    if base is None:
        raise LlmRequestError(
            f"unknown model key {model_key!r}; register it in MODELS first"
        )
    if db is None:
        return base

    try:
        effective = await get_effective_llm_config(db)
    except (DataDecryptError, DataEncryptionKeyMissingError) as exc:
        raise LlmRequestError("读取系统 LLM 配置失败，请检查密钥加密设置") from exc

    if model_key == "local-chat":
        return replace(
            base,
            base_url=effective.chat_base_url,
            api_key=effective.chat_api_key,
            request_model=effective.chat_model,
            input_cost_per_million_usd=effective.chat_input_cost_per_million_usd,
            output_cost_per_million_usd=effective.chat_output_cost_per_million_usd,
        )
    if model_key == "local-embedding":
        return replace(
            base,
            base_url=effective.embedding_base_url,
            api_key=effective.embedding_api_key,
            request_model=effective.embedding_model,
        )
    return base


@dataclass
class _StreamToolCallBuilder:
    """按 index 累积流式 tool_call 片段。"""

    id: str = ""
    name: str = ""
    arguments: str = ""


def _parse_chat_completion_body(body: dict[str, Any], config: ModelConfig) -> ChatResult:
    """解析非流式 /chat/completions JSON 为 ChatResult。"""
    try:
        choice = body["choices"][0]
        message = choice["message"]
        raw_tool_calls = message.get("tool_calls") or []
        tool_calls = [
            ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=tc["function"]["arguments"],
            )
            for tc in raw_tool_calls
        ]
        usage = body.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        return ChatResult(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice["finish_reason"],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=_cost_usd(config, prompt_tokens, completion_tokens),
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return _error_result("响应格式无效")


async def _consume_chat_sse(
    response: httpx.Response,
    config: ModelConfig,
    on_delta: ChatDeltaCallback | None,
) -> ChatResult:
    """
    消费 OpenAI 兼容 SSE，可选回调文本增量，返回聚合后的 ChatResult。

    Args:
        response: 已打开的流式响应（调用方负责关闭）
        config: 模型配置（算费用）
        on_delta: 每段 content 增量回调；可为 None

    Returns:
        聚合后的助手回合结果
    """
    content_parts: list[str] = []
    tool_builders: dict[int, _StreamToolCallBuilder] = {}
    finish_reason = "stop"
    prompt_tokens = 0
    completion_tokens = 0

    try:
        async for line in response.aiter_lines():
            if not line:
                continue
            if line.startswith(":"):
                # SSE 注释行（如 keep-alive）
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                return _error_result("响应格式无效")

            usage = chunk.get("usage") or {}
            if usage:
                prompt_tokens = int(usage.get("prompt_tokens", prompt_tokens) or prompt_tokens)
                completion_tokens = int(
                    usage.get("completion_tokens", completion_tokens) or completion_tokens
                )

            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])

            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                content_parts.append(piece)
                if on_delta is not None:
                    await on_delta(piece)

            for tc_delta in delta.get("tool_calls") or []:
                try:
                    index = int(tc_delta.get("index", 0))
                except (TypeError, ValueError):
                    index = 0
                builder = tool_builders.setdefault(index, _StreamToolCallBuilder())
                if tc_delta.get("id"):
                    builder.id = str(tc_delta["id"])
                function = tc_delta.get("function") or {}
                if function.get("name"):
                    builder.name = str(function["name"])
                if function.get("arguments"):
                    builder.arguments += str(function["arguments"])
    except httpx.RequestError:
        return _error_result("网络请求失败")

    tool_calls = [
        ToolCall(id=builder.id, name=builder.name, arguments=builder.arguments)
        for _, builder in sorted(tool_builders.items())
        if builder.id or builder.name
    ]
    content = "".join(content_parts) if content_parts else None
    return ChatResult(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=_cost_usd(config, prompt_tokens, completion_tokens),
    )


async def chat(
    model_key: str,
    messages: list[ChatMessage],
    *,
    tools: list[dict[str, Any]] | None = None,
    client: httpx.AsyncClient | None = None,
    stream: bool = False,
    on_delta: ChatDeltaCallback | None = None,
    db: AsyncSession | None = None,
) -> ChatResult:
    """
    发送一次 OpenAI 兼容 chat completion，返回助手回合。

    Args:
        model_key: MODELS 登记键
        messages: 对话历史
        tools: 可选 tools schema
        client: 可注入的 httpx 客户端（单测用）
        stream: False=整段返回（默认，兼容旧调用）；True=SSE 真 token 流
        on_delta: 仅 stream=True 时生效；每段文本增量回调
        db: 可选数据库会话；传入时读取系统配置覆盖 local-chat

    Returns:
        完整 ChatResult（流式时也是聚合结果）
    """
    config = await _resolve_model_config(model_key, db)
    if config.capability != "chat":
        raise LlmRequestError(f"model {model_key!r} is not registered for chat")

    payload: dict[str, Any] = {
        "model": config.request_model,
        "messages": [_message_to_payload(m) for m in messages],
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
    if stream:
        # 尽量要到 usage（OpenAI / 部分兼容实现支持；不支持则保持 0）
        payload["stream_options"] = {"include_usage": True}

    # 注入的客户端（单测用）由调用方负责生命周期；共享客户端在 lifespan 关停时
    # 由 close_llm_clients 统一释放，这里不能逐次 aclose，否则连接池形同虚设。
    http_client = client if client is not None else _shared_client(config)
    if not stream:
        try:
            response = await http_client.post("/chat/completions", json=payload)
        except httpx.RequestError:
            return _error_result("网络请求失败")
        if response.status_code != 200:
            return _error_result(_http_error_reason(response.status_code, response.text))
        try:
            body = response.json()
        except ValueError:
            return _error_result("响应格式无效")
        return _parse_chat_completion_body(body, config)

    try:
        async with http_client.stream("POST", "/chat/completions", json=payload) as response:
            if response.status_code != 200:
                error_text = (await response.aread()).decode("utf-8", errors="replace")
                return _error_result(_http_error_reason(response.status_code, error_text))
            return await _consume_chat_sse(response, config, on_delta)
    except httpx.RequestError:
        return _error_result("网络请求失败")


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """The embedding vectors returned by `embed()`, in request order."""

    vectors: list[list[float]]
    prompt_tokens: int


async def embed(
    model_key: str,
    inputs: list[str],
    *,
    client: httpx.AsyncClient | None = None,
    db: AsyncSession | None = None,
) -> EmbeddingResult:
    """
    发送一次 OpenAI 兼容 embeddings 请求，按输入顺序返回向量。

    Args:
        model_key: MODELS 登记键
        inputs: 待嵌入文本列表
        client: 可注入的 httpx 客户端（单测用）
        db: 可选数据库会话；传入时读取系统配置覆盖 local-embedding

    Returns:
        向量与 token 用量
    """
    config = await _resolve_model_config(model_key, db)
    if config.capability != "embedding":
        raise LlmRequestError(f"model {model_key!r} is not registered for embedding")

    payload: dict[str, Any] = {"model": config.request_model, "input": inputs}

    http_client = client if client is not None else _shared_client(config)
    try:
        response = await http_client.post("/embeddings", json=payload)
    except httpx.RequestError as exc:
        raise LlmRequestError(f"model {model_key!r} 网络请求失败") from exc

    # 与 chat() 一致：错误正文必须截断并脱敏后才能进异常消息。这个异常会被
    # kb_semantic_search 拼进 ToolResult 回灌给模型，原样透传上游正文可能把
    # 被网关回显的 Authorization 头带进模型上下文。
    if response.status_code != 200:
        raise LlmRequestError(
            f"model {model_key!r} 调用失败：{_http_error_reason(response.status_code, response.text)}"
        )

    try:
        body = response.json()
        ordered = sorted(body["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in ordered]
        usage = body.get("usage", {})
        return EmbeddingResult(vectors=vectors, prompt_tokens=usage.get("prompt_tokens", 0))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LlmRequestError(f"model {model_key!r} 返回的 embedding 响应格式无效") from exc
