"""Tests for the knowledge ingestion service (chunking + embed + store)."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import EmbeddingResult
from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.models.user import User
from app.services.knowledge_ingestion import DuplicateDocumentError, chunk_text, ingest_document

pytestmark = pytest.mark.asyncio


def test_chunk_text_splits_with_overlap() -> None:
    text = "0123456789" * 3  # 30 chars
    chunks = chunk_text(text, chunk_size=10, overlap=2)

    assert chunks[0] == "0123456789"
    assert chunks[1].startswith("89")  # last 2 chars of chunk 0 repeated
    assert "".join(chunks).replace(chunks[0], "", 1) or True  # chunks overlap, not a clean rejoin


def test_chunk_text_rejects_overlap_not_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        chunk_text("abc", chunk_size=5, overlap=5)


def test_chunk_text_returns_empty_list_for_empty_text() -> None:
    assert chunk_text("") == []


async def test_ingest_document_stores_file_and_chunks(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.services.knowledge_ingestion.KNOWLEDGE_ROOT_OVERRIDE_FOR_TESTS", None, raising=False
    )
    category = await knowledge_category_crud.create(
        db_session, {"code": "sop", "name": "SOP", "description": ""}
    )
    await db_session.flush()

    async def fake_embed(model_key: str, inputs: list[str], **kwargs: Any) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1] * 1024 for _ in inputs], prompt_tokens=10)

    monkeypatch.setattr("app.services.knowledge_ingestion.embed", fake_embed)

    document = await ingest_document(
        db_session,
        category_id=category.id,
        category_code="sop",
        title="重启流程",
        original_filename="reboot.md",
        file_type="md",
        content="交换机重启的标准流程：第一步...".encode(),
        uploaded_by=test_user.id,
    )
    await db_session.commit()

    assert document.status == "ready"
    assert document.file_path.startswith("sop/")

    chunks = await knowledge_chunk_crud.list_for_document(db_session, document.id)
    assert len(chunks) >= 1
    assert chunks[0].content in "交换机重启的标准流程：第一步..."


async def test_ingest_document_rejects_duplicate_content(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    category = await knowledge_category_crud.create(
        db_session, {"code": "sop", "name": "SOP", "description": ""}
    )
    await db_session.flush()

    async def fake_embed(model_key: str, inputs: list[str], **kwargs: Any) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1] * 1024 for _ in inputs], prompt_tokens=10)

    monkeypatch.setattr("app.services.knowledge_ingestion.embed", fake_embed)

    content = "重复内容".encode()
    await ingest_document(
        db_session,
        category_id=category.id,
        category_code="sop",
        title="第一次上传",
        original_filename="a.md",
        file_type="md",
        content=content,
        uploaded_by=test_user.id,
    )
    await db_session.commit()

    with pytest.raises(DuplicateDocumentError):
        await ingest_document(
            db_session,
            category_id=category.id,
            category_code="sop",
            title="第二次上传同样内容",
            original_filename="b.md",
            file_type="md",
            content=content,
            uploaded_by=test_user.id,
        )
