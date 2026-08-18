"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: monitor.py
@DateTime: 2026-08-13 14:00
@Docs: 监控目标请求与响应模型
"""

from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from app.schemas.common import ApiModel

type MonitorLatestStatus = Literal["up", "down"]


class MonitorTargetCreate(ApiModel):
    """创建监控目标。"""

    ip_address: str = Field(min_length=1, max_length=45)
    port: int = Field(ge=1, le=65535)
    label: str = Field(default="", max_length=100)
    check_interval_seconds: int = Field(default=30, ge=5, le=3600)
    is_active: bool = True
    cmdb_asset_id: int | None = Field(default=None, gt=0)


class MonitorTargetUpdate(ApiModel):
    """部分更新监控目标；未出现的字段保持原值。"""

    ip_address: str | None = Field(default=None, min_length=1, max_length=45)
    port: int | None = Field(default=None, ge=1, le=65535)
    label: str | None = Field(default=None, max_length=100)
    check_interval_seconds: int | None = Field(default=None, ge=5, le=3600)
    is_active: bool | None = None
    cmdb_asset_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def reject_empty_update(self) -> Self:
        """至少提供一个要更新的字段。"""
        if not self.model_fields_set:
            raise ValueError("至少提供一个要更新的字段")
        return self


type MonitorBucketState = Literal["up", "down", "unknown"]


class MonitorUptimeWindow(ApiModel):
    """最近一小时的可用率状态条：60 个分钟格 + 可用率。

    三个字段绑在一起才有意义，所以做成嵌套对象而不是摊平：
    单看 `buckets` 不知道它从哪个时刻开始、每格多长。前端靠
    `started_at + index * bucket_seconds` 算出每格对应的时间做 tooltip。
    """

    started_at: datetime
    bucket_seconds: int
    buckets: list[MonitorBucketState]
    # 窗口内一次探测都没有时为 None，而不是 1.0——
    # 一个刚建好、从没跑过的目标显示「100% 可用」是撒谎。
    uptime_rate: float | None


class MonitorTargetResponse(ApiModel):
    """监控目标公开表示，附带最近一次探测结果与最近一小时的可用率状态条。"""

    id: int
    cmdb_asset_id: int | None
    ip_address: str
    port: int
    label: str
    check_interval_seconds: int
    is_active: bool
    created_at: datetime
    latest_status: MonitorLatestStatus | None = None
    latest_latency_ms: int | None = None
    latest_detail: str = ""
    latest_checked_at: datetime | None = None
    uptime_window: MonitorUptimeWindow

    model_config = ConfigDict(from_attributes=True)


class MonitorRuntimeResponse(ApiModel):
    """监控页轮询所需的运行参数。"""

    sweep_interval_seconds: int


class MonitorLogItem(ApiModel):
    """单条监控状态变化日志。"""

    id: int
    target_id: int
    label: str
    ip_address: str
    port: int
    status: MonitorLatestStatus
    latency_ms: int | None = None
    detail: str = ""
    checked_at: datetime
