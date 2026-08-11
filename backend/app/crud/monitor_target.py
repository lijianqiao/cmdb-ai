"""CRUD operations for monitor targets."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
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


monitor_target_crud = CRUDMonitorTarget()
