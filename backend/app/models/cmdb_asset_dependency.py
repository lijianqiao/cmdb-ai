"""CMDB asset dependency edge — e.g. a switch (parent) hosting servers (children).

Composite primary key, no surrogate id, matching this project's existing
relation-table convention (see UserRole/RolePermission).
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CmdbAssetDependency(Base):
    """One directed dependency edge between two CMDB assets."""

    __tablename__ = "cmdb_asset_dependencies"

    parent_asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cmdb_assets.id", ondelete="CASCADE"), primary_key=True
    )
    child_asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cmdb_assets.id", ondelete="CASCADE"), primary_key=True
    )
    relation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return (
            f"<CmdbAssetDependency(parent={self.parent_asset_id}, "
            f"child={self.child_asset_id}, relation={self.relation_type!r})>"
        )
