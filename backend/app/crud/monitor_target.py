"""CRUD operations for monitor targets."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase, contains_pattern
from app.models.monitor_target import MonitorTarget


def _escape_like_literal(value: str) -> str:
    """Escape a literal string for safe use inside a SQL LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class CRUDMonitorTarget(CRUDBase[MonitorTarget]):
    """Monitor target persistence; generic get/create/update come from CRUDBase.

    This model has no is_deleted column, so soft_delete() is simply unused.
    """

    model = MonitorTarget

    async def list_active(self, db: AsyncSession) -> list[MonitorTarget]:
        """Return every target the sweep should probe this round."""
        stmt = select(MonitorTarget).where(MonitorTarget.is_active.is_(True))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_ip_prefix(self, db: AsyncSession, ip_prefix: str) -> list[MonitorTarget]:
        """Return targets whose IP starts with `ip_prefix` (literal prefix match, not CIDR math)."""
        pattern = f"{_escape_like_literal(ip_prefix)}%"
        stmt = select(MonitorTarget).where(MonitorTarget.ip_address.like(pattern, escape="\\"))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_ip_port(
        self,
        db: AsyncSession,
        ip_address: str,
        port: int,
        *,
        exclude_id: int | None = None,
    ) -> MonitorTarget | None:
        """按 IP + 端口查找一条目标；更新时可用 exclude_id 排除自身。"""
        stmt = select(MonitorTarget).where(
            MonitorTarget.ip_address == ip_address,
            MonitorTarget.port == port,
        )
        if exclude_id is not None:
            stmt = stmt.where(MonitorTarget.id != exclude_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_multi_filtered(
        self,
        db: AsyncSession,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        skip: int = 0,
        limit: int = 10,
    ) -> tuple[list[MonitorTarget], int]:
        """分页列出监控目标，供管理页使用。"""
        stmt = select(MonitorTarget)
        if search:
            pattern = contains_pattern(search)
            stmt = stmt.where(
                MonitorTarget.ip_address.ilike(pattern, escape="\\")
                | MonitorTarget.label.ilike(pattern, escape="\\")
            )
        if is_active is not None:
            stmt = stmt.where(MonitorTarget.is_active.is_(is_active))

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await db.execute(count_stmt)).scalar_one()

        page_stmt = stmt.order_by(MonitorTarget.id.desc()).offset(skip).limit(limit)
        targets = list((await db.execute(page_stmt)).scalars().all())
        return targets, total

    async def hard_delete(self, db: AsyncSession, target_id: int) -> bool:
        """物理删除监控目标（探测记录依赖库级 CASCADE）。

        Args:
            db: 数据库会话
            target_id: 目标主键

        Returns:
            找到并删除返回 True，否则 False
        """
        target = await self.get(db, target_id)
        if target is None:
            return False
        await db.delete(target)
        await db.flush()
        return True


monitor_target_crud = CRUDMonitorTarget()
