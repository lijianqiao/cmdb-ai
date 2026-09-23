"""CMDB asset — a lightweight configuration-item record.

Not a full ITIL CMDB: just enough fields to answer "who owns this / where is
it / what business system does it belong to" (docs/AGENT_ARCHITECTURE.md §3).
Credential fields hold only an encrypted static password (see
app/core/cmdb_credential.py) or a bare username for dynamic (OTP-style)
credentials — dynamic passwords are never persisted anywhere.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class CmdbAsset(Base, TimestampMixin):
    """One managed network device (switch, router, firewall, ...)."""

    __tablename__ = "cmdb_assets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    asset_type: Mapped[str] = mapped_column(String(50), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False, index=True)
    location: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    owner_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    business_system: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    subnet_cidr: Mapped[str] = mapped_column(String(45), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    vendor: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    credential_type: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    credential_username: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    credential_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 管理口的 SSH 端口；改过端口的设备不填对就连不上。
    ssh_port: Mapped[int] = mapped_column(Integer, nullable=False, default=22, server_default="22")
    # 思科用户级登录后执行 enable 用的口令：none / static（D6 这一版不做 dynamic）。
    # 密文和登录密码同一套加密；其它厂商没有 enable 这一步，登记了也不会用。
    enable_credential_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="none", server_default="none"
    )
    enable_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    def __repr__(self) -> str:
        return f"<CmdbAsset(id={self.id}, hostname={self.hostname!r}, ip={self.ip_address!r})>"
