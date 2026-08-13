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


class MonitorTargetResponse(ApiModel):
    """监控目标公开表示，附带最近一次探测结果。"""

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

    model_config = ConfigDict(from_attributes=True)


class MonitorRuntimeResponse(ApiModel):
    """监控页轮询所需的运行参数。"""

    sweep_interval_seconds: int
