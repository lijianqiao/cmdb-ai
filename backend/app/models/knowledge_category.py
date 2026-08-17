"""Knowledge-base category — the top-level grouping for uploaded documents."""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# 「未分类」是上传时没指定分类的收纳桶，不是一个真实的业务分类。
# 常量放在模型层，是因为 API（创建兜底分类）和分类建议服务（把它排除出候选）
# 都要用；由 API 层定义再被服务层反向 import 是层次颠倒。
UNCATEGORIZED_CODE = "uncategorized"
UNCATEGORIZED_NAME = "未分类"


class KnowledgeCategory(Base, TimestampMixin):
    """One knowledge category (e.g. SOP, network topology, vendor manuals)."""

    __tablename__ = "knowledge_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    def __repr__(self) -> str:
        return f"<KnowledgeCategory(id={self.id}, code={self.code!r})>"
