"""Knowledge-base document — metadata only; the file content lives on disk
under knowledge/{category_code}/{document_id}_{filename} (see
app/services/knowledge_storage.py), per docs/AGENT_ARCHITECTURE.md §4.3.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
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
    # 目前**恒为 "ready"**：ingest_document 在同一个事务里把它从 processing 改成
    # ready，外部永远观察不到中间态，全库也没有任何查询按它过滤。留着不是因为它
    # 现在有用，而是将来若要做「上传后需审批才可检索」，这里是它的天然落脚点，
    # 届时只需扩枚举 + 在检索侧加过滤，不必再加列。**读它之前先确认它真的被写过。**
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    uploaded_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # AI 分类建议。与 category_id 分开存：建议只是建议，必须由人确认后才改变
    # 文档的真实归属（应用建议 = 把 suggested_category_id 写进 category_id 并清空建议）。
    # 分类被删除时置空而不是级联删文档，所以用 SET NULL。
    suggested_category_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("knowledge_categories.id", ondelete="SET NULL"), nullable=True
    )
    suggestion_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    suggestion_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suggested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    def __repr__(self) -> str:
        return f"<KnowledgeDocument(id={self.id}, title={self.title!r}, status={self.status!r})>"
