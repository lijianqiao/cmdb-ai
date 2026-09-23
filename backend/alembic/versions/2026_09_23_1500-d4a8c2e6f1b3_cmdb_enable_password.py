"""Record an optional enable password on CMDB assets (Cisco).

Revision ID: d4a8c2e6f1b3
Revises: b7e2d4f1c3a9
Create Date: 2026-09-23 15:00:00+00:00

P3（D6）：思科设备用户级登录后要执行 enable 才能看配置、改配置。原来只能存一个
登录密码，用户级账号的 show running-config 和端口启停都会失败。

1. enable_credential_type：none / static（字符串列，库级默认 none）。已有资产一律
   按「没有 enable 口令」回填，连接行为和升级前一样。D6 这一版只做 none / static；
   动态 enable 以后要做时往这列加 dynamic 取值即可，不用再迁移。
2. enable_password_encrypted：静态 enable 口令的密文，和登录密码同一套加密；可空。
3. 回退会丢掉已登记的 enable 口令密文，要显式确认。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "d4a8c2e6f1b3"
down_revision: str | None = "b7e2d4f1c3a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_destructive_downgrade() -> None:
    """删掉 enable 口令两列会丢掉已登记的密文，必须显式确认。"""
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    """Add the enable credential type (default none) and its ciphertext."""
    op.add_column(
        "cmdb_assets",
        sa.Column(
            "enable_credential_type", sa.String(length=20), nullable=False, server_default="none"
        ),
    )
    op.add_column(
        "cmdb_assets", sa.Column("enable_password_encrypted", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    """Drop both enable columns after an explicit destructive opt-in."""
    _require_destructive_downgrade()
    op.drop_column("cmdb_assets", "enable_password_encrypted")
    op.drop_column("cmdb_assets", "enable_credential_type")
