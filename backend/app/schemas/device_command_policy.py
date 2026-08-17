"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: device_command_policy.py
@DateTime: 2026-08-12 22:15
@Docs: 设备命令策略请求与响应模型
"""

from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from app.agent.device_commands import command_type_of, list_device_commands
from app.schemas.common import ApiModel

type PolicyScope = Literal["asset_type", "asset"]
type PolicyDecision = Literal["whitelist", "blacklist"]

_VALID_COMMAND_NAMES = frozenset(item.name for item in list_device_commands())


def _validate_command_name(value: str) -> str:
    if value not in _VALID_COMMAND_NAMES:
        raise ValueError(f"未知命令名：{value}")
    return value


class DeviceCommandPolicyCreate(ApiModel):
    """Create a device command policy."""

    scope: PolicyScope
    asset_type: str | None = Field(default=None, max_length=50)
    asset_id: int | None = None
    command_name: str = Field(min_length=1, max_length=100)
    decision: PolicyDecision
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_command_name(self) -> Self:
        _validate_command_name(self.command_name)
        return self

    @model_validator(mode="after")
    def validate_scope_fields(self) -> Self:
        if self.scope == "asset_type":
            if not self.asset_type:
                raise ValueError("scope 为 asset_type 时必须填写 asset_type")
            if self.asset_id is not None:
                raise ValueError("scope 为 asset_type 时不能填写 asset_id")
            if command_type_of(self.command_name) == "state_changing":
                raise ValueError(
                    "变更类命令（reboot/shutdown/port_enable/port_disable）只能按单台设备（scope=asset）"
                    "配置白/黑名单，不允许按设备类型一次性放行"
                )
        else:
            if self.asset_id is None:
                raise ValueError("scope 为 asset 时必须填写 asset_id")
            if self.asset_type is not None:
                raise ValueError("scope 为 asset 时不能填写 asset_type")
        return self


class DeviceCommandPolicyUpdate(ApiModel):
    """Partially update a device command policy (only decision/note are mutable)."""

    decision: PolicyDecision | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def reject_empty_update(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("至少提供一个要更新的字段")
        return self


class CmdbAssetBrief(ApiModel):
    """关联的 CMDB 资产基础信息摘要。"""

    id: int
    hostname: str
    ip_address: str
    asset_type: str

    model_config = ConfigDict(from_attributes=True)


class DeviceCommandPolicyResponse(ApiModel):
    """Public policy representation."""

    id: int
    scope: PolicyScope
    asset_type: str | None
    asset_id: int | None
    command_name: str
    decision: PolicyDecision
    note: str
    created_by_user_id: int | None
    created_at: datetime
    updated_at: datetime
    asset: CmdbAssetBrief | None = None

    model_config = ConfigDict(from_attributes=True)
