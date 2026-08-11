"""Tests for kb_semantic_search — embeds the query, then delegates to pgvector search.

Uses a fake embed() (no real LLM call) and a fake search_similar() (no real
pgvector query) so this test runs on the standard aiosqlite test DB without
needing TEST_POSTGRES_DATABASE_URL — the actual cosine-distance behavior is
covered separately by Task 4's Postgres-gated test.
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.knowledge_tools import kb_semantic_search
from app.core.llm import EmbeddingResult
from app.models.knowledge_chunk import KnowledgeChunk

pytestmark = pytest.mark.asyncio


async def test_kb_semantic_search_returns_formatted_results(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_embed(model_key: str, inputs: list[str], **kwargs: Any) -> EmbeddingResult:
        assert inputs == ["交换机怎么重启"]
        return EmbeddingResult(vectors=[[0.1] * 1024], prompt_tokens=5)

    async def fake_search_similar(
        db: AsyncSession, *, query_embedding: list[float], category_id: int | None, top_k: int
    ) -> list[tuple[KnowledgeChunk, float]]:
        assert query_embedding == [0.1] * 1024
        chunk = KnowledgeChunk(
            id=1, document_id=1, chunk_index=0, content="先断电再通电", token_count=6, embedding=[0.1] * 1024
        )
        return [(chunk, 0.05)]

    monkeypatch.setattr("app.agent.knowledge_tools.embed", fake_embed)
    monkeypatch.setattr(
        "app.agent.knowledge_tools.knowledge_chunk_crud.search_similar", fake_search_similar
    )

    result = await kb_semantic_search(db_session, "交换机怎么重启")

    assert result.control == "ok"
    assert "先断电再通电" in result.content
    assert "document_id=1" in result.content


async def test_kb_semantic_search_reports_no_results(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_embed(model_key: str, inputs: list[str], **kwargs: Any) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1] * 1024], prompt_tokens=5)

    async def fake_search_similar(
        db: AsyncSession, *, query_embedding: list[float], category_id: int | None, top_k: int
    ) -> list[tuple[KnowledgeChunk, float]]:
        return []

    monkeypatch.setattr("app.agent.knowledge_tools.embed", fake_embed)
    monkeypatch.setattr(
        "app.agent.knowledge_tools.knowledge_chunk_crud.search_similar", fake_search_similar
    )

    result = await kb_semantic_search(db_session, "没有相关内容的问题")

    assert result.control == "ok"
    assert result.content == "没有找到相关内容"


async def test_kb_semantic_search_reports_embedding_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.llm import LlmRequestError

    async def failing_embed(model_key: str, inputs: list[str], **kwargs: Any) -> EmbeddingResult:
        raise LlmRequestError("embedding 服务不可用")

    monkeypatch.setattr("app.agent.knowledge_tools.embed", failing_embed)

    result = await kb_semantic_search(db_session, "任意问题")

    assert result.control == "failed"
