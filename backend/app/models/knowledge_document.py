"""Knowledge-base document — metadata only; the file content lives on disk
under knowledge/{category_code}/{document_id}_{filename} (see
app/services/knowledge_storage.py), per docs/AGENT_ARCHITECTURE.md §4.3.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class KnowledgeDocument(Base, TimestampMixin):
    """One uploaded document's metadata."""

    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("knowledge_categories.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    uploaded_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    def __repr__(self) -> str:
        return f"<KnowledgeDocument(id={self.id}, title={self.title!r}, status={self.status!r})>"
