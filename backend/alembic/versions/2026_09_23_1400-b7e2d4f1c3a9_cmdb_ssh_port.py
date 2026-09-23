"""Record a per-device SSH port on CMDB assets.

Revision ID: b7e2d4f1c3a9
Revises: a1c4e8b2d907
Create Date: 2026-09-23 14:00:00+00:00

P3：设备的 SSH 端口原来写死 22，管理口改过端口的设备根本连不上。

1. cmdb_assets 新增 ssh_port（整数、非空），库级默认 22：已有的每台设备都按 22
   回填，连接行为和升级前完全一样。取值范围 1–65535 由接口层校验。
2. 回退会丢掉每台设备登记的端口，所以和其它会丢数据的回退一样要显式确认。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "b7e2d4f1c3a9"
down_revision: str | None = "a1c4e8b2d907"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_destructive_downgrade() -> None:
    """删掉 ssh_port 会丢掉改过端口的登记，必须显式确认。"""
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    """Add ssh_port, backfilling every existing asset with 22."""
    op.add_column(
        "cmdb_assets",
        sa.Column("ssh_port", sa.Integer(), nullable=False, server_default="22"),
    )


def downgrade() -> None:
    """Drop ssh_port after an explicit destructive opt-in."""
    _require_destructive_downgrade()
    op.drop_column("cmdb_assets", "ssh_port")
