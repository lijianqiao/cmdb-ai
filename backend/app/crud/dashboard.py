"""Dashboard aggregate queries."""

from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cmdb_asset import CmdbAsset
from app.models.device_command_policy import DeviceCommandPolicy
from app.models.hitl_proposal import HitlProposal
from app.models.monitor_status_event import MonitorStatusEvent
from app.models.monitor_target import MonitorTarget
from app.models.permission import Permission
from app.models.role import Role
from app.models.user import User


class DashboardCounts(TypedDict):
    user_count: int
    role_count: int
    permission_count: int
    active_user_count: int
    cmdb_asset_count: int
    monitor_target_count: int
    monitor_down_count: int
    pending_hitl_count: int
    device_command_policy_count: int


class CRUDDashboard:
    """Fetch dashboard counters in one database round trip."""

    async def get_counts(self, db: AsyncSession) -> DashboardCounts:
        """汇总用户与运维对象数量，供仪表盘卡片使用。"""
        user_counts = (
            select(
                func.count(User.id).label("user_count"),
                func.count(User.id).filter(User.is_active.is_(True)).label("active_user_count"),
            )
            .where(User.is_deleted.is_(False))
            .subquery()
        )
        role_count = select(func.count(Role.id)).where(Role.is_deleted.is_(False)).scalar_subquery()
        permission_count = (
            select(func.count(Permission.id))
            .where(Permission.is_deleted.is_(False))
            .scalar_subquery()
        )
        cmdb_asset_count = (
            select(func.count(CmdbAsset.id)).where(CmdbAsset.is_deleted.is_(False)).scalar_subquery()
        )
        monitor_target_count = select(func.count(MonitorTarget.id)).scalar_subquery()
        pending_hitl_count = (
            select(func.count(HitlProposal.id)).where(HitlProposal.status == "PENDING").scalar_subquery()
        )
        device_command_policy_count = (
            select(func.count(DeviceCommandPolicy.id))
            .where(DeviceCommandPolicy.is_deleted.is_(False))
            .scalar_subquery()
        )

        row_number = (
            func.row_number()
            .over(
                partition_by=MonitorStatusEvent.target_id,
                order_by=(MonitorStatusEvent.checked_at.desc(), MonitorStatusEvent.id.desc()),
            )
            .label("rn")
        )
        ranked = select(MonitorStatusEvent.target_id, MonitorStatusEvent.status, row_number).subquery()
        monitor_down_count = (
            select(func.count())
            .select_from(ranked)
            .where(ranked.c.rn == 1, ranked.c.status == "down")
            .scalar_subquery()
        )

        result = await db.execute(
            select(
                user_counts.c.user_count,
                role_count.label("role_count"),
                permission_count.label("permission_count"),
                user_counts.c.active_user_count,
                cmdb_asset_count.label("cmdb_asset_count"),
                monitor_target_count.label("monitor_target_count"),
                monitor_down_count.label("monitor_down_count"),
                pending_hitl_count.label("pending_hitl_count"),
                device_command_policy_count.label("device_command_policy_count"),
            )
        )
        row = result.one()
        return {
            "user_count": row.user_count,
            "role_count": row.role_count,
            "permission_count": row.permission_count,
            "active_user_count": row.active_user_count,
            "cmdb_asset_count": row.cmdb_asset_count,
            "monitor_target_count": row.monitor_target_count,
            "monitor_down_count": row.monitor_down_count,
            "pending_hitl_count": row.pending_hitl_count,
            "device_command_policy_count": row.device_command_policy_count,
        }


dashboard_crud = CRUDDashboard()
