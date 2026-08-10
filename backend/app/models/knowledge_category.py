"""Knowledge-base category — the top-level grouping for uploaded documents."""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class KnowledgeCategory(Base, TimestampMixin):
    """One knowledge category (e.g. SOP, network topology, vendor manuals)."""

    __tablename__ = "knowledge_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    def __repr__(self) -> str:
        return f"<KnowledgeCategory(id={self.id}, code={self.code!r})>"
