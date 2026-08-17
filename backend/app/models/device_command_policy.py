"""设备命令白/黑名单策略——只决定"要不要跳过审批"，不决定命令内容。

命令字符串本身固定在 app/agent/device_commands.py 这个代码层目录里；这张
表只是给一个 (设备类型 或 单台设备, 命令名) 组合打一个 whitelist/blacklist
标签。单台设备的策略永远覆盖设备类型级别的策略，不管方向，查找顺序见
app/crud/device_command_policy.py::resolve_policy。
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.cmdb_asset import CmdbAsset


class DeviceCommandPolicy(Base, TimestampMixin):
    """一条设备命令白/黑名单策略。"""

    __tablename__ = "device_command_policies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    asset_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    asset_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cmdb_assets.id", ondelete="CASCADE"), nullable=True
    )
    command_name: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    asset: Mapped[CmdbAsset | None] = relationship(
        "CmdbAsset", lazy="joined", foreign_keys=[asset_id]
    )

    def __repr__(self) -> str:
        return (
            f"<DeviceCommandPolicy(id={self.id}, scope={self.scope!r}, "
            f"command={self.command_name!r}, decision={self.decision!r})>"
        )
