"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: system_config.py
@DateTime: 2026-08-13 14:00
@Docs: 系统运行配置持久化模型，仅存储白名单内的配置键。
"""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SystemConfig(Base, TimestampMixin):
    """一条系统运行配置键值对。"""

    __tablename__ = "system_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<SystemConfig(key={self.key!r}, value={'***' if self.value else None})>"
