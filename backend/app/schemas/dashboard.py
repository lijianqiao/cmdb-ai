"""仪表盘统计 Schema。"""

from datetime import datetime

from pydantic import Field

from app.schemas.common import ApiModel


class DashboardStats(ApiModel):
    """仪表盘统计数据。"""

    user_count: int = Field(default=0, description="用户总数")
    role_count: int = Field(default=0, description="角色总数")
    permission_count: int = Field(default=0, description="权限总数")
    active_user_count: int = Field(default=0, description="启用用户数")
    cmdb_asset_count: int = Field(default=0, description="CMDB 资产数")
    monitor_target_count: int = Field(default=0, description="监控目标数")
    monitor_down_count: int = Field(default=0, description="最近探测为离线的目标数")
    pending_hitl_count: int = Field(default=0, description="待审批 HITL 提案数")
    device_command_policy_count: int = Field(default=0, description="设备命令策略数")


class RecentLoginItem(ApiModel):
    """最近登录记录。"""

    id: int
    user_id: int | None = None
    username: str | None = None
    action: str
    ip: str
    created_at: datetime


class DashboardData(ApiModel):
    """仪表盘完整数据。"""

    stats: DashboardStats
    recent_logs: list[RecentLoginItem] = Field(default_factory=list)
