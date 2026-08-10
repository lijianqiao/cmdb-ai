"""Knowledge-base chunk — one retrieval unit with its embedding vector.

1024 dims matches the local Qwen3-Embedding-0.6B model (see
docs/AGENT_ARCHITECTURE.md assumption A2; adjust the column definition if the
actual embedding model changes).
"""

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

EMBEDDING_DIM = 1024


class KnowledgeChunk(Base):
    """One chunk of a document's text plus its embedding vector."""

    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"<KnowledgeChunk(id={self.id}, document_id={self.document_id}, chunk_index={self.chunk_index})>"
