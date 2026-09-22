"""DeviceCommandPolicyCreate schema：state_changing 命令 scope 安全闸门。"""

import pytest
from pydantic import ValidationError

from app.agent.device_commands import list_device_commands
from app.schemas.device_command_policy import DeviceCommandPolicyCreate


def test_state_changing_command_rejects_asset_type_scope() -> None:
    with pytest.raises(ValidationError, match=r"变更类命令.*scope.*asset") as exc_info:
        DeviceCommandPolicyCreate(
            scope="asset_type",
            asset_type="switch",
            command_name="reboot",
            decision="whitelist",
        )

    # 报错文案里的命令清单从目录生成：目录加减命令时不用回来改这句话。
    message = str(exc_info.value)
    for item in list_device_commands():
        if item.command_type == "state_changing":
            assert item.name in message, item.name


def test_state_changing_command_accepts_asset_scope() -> None:
    policy = DeviceCommandPolicyCreate(
        scope="asset",
        asset_id=1,
        command_name="reboot",
        decision="whitelist",
    )
    assert policy.command_name == "reboot"


def test_asset_type_scope_rejects_non_network_asset_type() -> None:
    """CMDB 只登记网络设备，按服务器类型建策略没有意义，直接拒绝。"""
    with pytest.raises(ValidationError):
        DeviceCommandPolicyCreate(
            scope="asset_type",
            asset_type="server",
            command_name="show_version",
            decision="whitelist",
        )


def test_retired_shutdown_command_is_rejected() -> None:
    with pytest.raises(ValidationError, match="未知命令名"):
        DeviceCommandPolicyCreate(
            scope="asset",
            asset_id=1,
            command_name="shutdown",
            decision="blacklist",
        )


def test_read_only_command_still_accepts_asset_type_scope() -> None:
    """回归：只读命令不受这条新规则影响。"""
    policy = DeviceCommandPolicyCreate(
        scope="asset_type",
        asset_type="switch",
        command_name="show_version",
        decision="whitelist",
    )
    assert policy.scope == "asset_type"
