"""Chunking + embedding + storage orchestration for uploaded documents.

Ties together app.services.knowledge_storage (file I/O), app.core.llm.embed
(vectors), and the knowledge_document_crud/knowledge_chunk_crud CRUD layer.
Only flushes — the caller (the upload API route) commits once, after this
and its audit-log entry both succeed, per this project's transaction
convention.
"""

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import embed
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.models.knowledge_document import KnowledgeDocument
from app.services.knowledge_storage import delete_document_file, write_document_file

_DEFAULT_CHUNK_SIZE = 800
_DEFAULT_CHUNK_OVERLAP = 100


class DuplicateDocumentError(ValueError):
    """Raised when a document with identical content already exists (active, not deleted).

    去重是**全库范围**的，不按分类隔离：同一份内容换个分类再传一次，检索时会命中
    两份一模一样的切片，等于稀释召回质量。

    带上标题是因为只报 id 等于没报——用户看到 "id=4" 无从知道撞的是哪一份，
    也就无法判断该放弃上传还是该先删掉旧的那份。
    """

    def __init__(self, document_id: int, title: str = "") -> None:
        self.document_id = document_id
        self.title = title
        super().__init__(
            f"a document with identical content already exists: id={document_id}"
        )


def chunk_text(
    text: str,
    *,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split text into overlapping fixed-size character chunks (CJK-safe: character-based,
    not word-boundary-based).
    """
    if chunk_size <= overlap:
        raise ValueError("chunk_size must be greater than overlap")
    if not text:
        return []

    chunks: list[str] = []
    start = 0
    text_length = len(text)
    while start < text_length:
        end = min(start + chunk_size, text_length)
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end == text_length:
            break
        start = end - overlap
    return chunks


async def ingest_document(
    db: AsyncSession,
    *,
    category_id: int,
    category_code: str,
    title: str,
    original_filename: str,
    file_type: str,
    content: bytes,
    uploaded_by: int,
    embedding_model_key: str = "local-embedding",
) -> KnowledgeDocument:
    """Store a document's file, chunk it, embed each chunk, and store the chunks.

    Raises DuplicateDocumentError if an active document with the same content
    hash already exists — the caller decides how to surface that (this
    project's convention: translate it to an HTTP 409 in the route layer).
    """
    content_hash = hashlib.sha256(content).hexdigest()
    existing = await knowledge_document_crud.get_by_content_hash(db, content_hash)
    if existing is not None:
        raise DuplicateDocumentError(existing.id, existing.title)

    document = await knowledge_document_crud.create(
        db,
        {
            "category_id": category_id,
            "title": title,
            "original_filename": original_filename,
            "file_path": "",
            "file_type": file_type,
            "content_hash": content_hash,
            "status": "processing",
            "uploaded_by": uploaded_by,
        },
    )
    await db.flush()

    relative_path = write_document_file(
        category_code=category_code,
        document_id=document.id,
        filename=original_filename,
        content=content,
    )
    try:
        updated = await knowledge_document_crud.update(db, document.id, {"file_path": relative_path})
        assert updated is not None  # just created it; cannot be missing

        text = content.decode("utf-8")
        chunks = chunk_text(text)
        if chunks:
            embedding_result = await embed(embedding_model_key, chunks, db=db)
            for index, (chunk_content, vector) in enumerate(
                zip(chunks, embedding_result.vectors, strict=True)
            ):
                await knowledge_chunk_crud.create(
                    db,
                    document_id=document.id,
                    chunk_index=index,
                    content=chunk_content,
                    token_count=len(chunk_content),
                    embedding=vector,
                )

        ready = await knowledge_document_crud.update(db, document.id, {"status": "ready"})
        assert ready is not None
        return ready
    except Exception:
        delete_document_file(relative_path)
        raise
