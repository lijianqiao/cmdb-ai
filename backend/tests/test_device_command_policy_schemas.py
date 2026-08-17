"""DeviceCommandPolicyCreate schema：state_changing 命令 scope 安全闸门。"""

import pytest
from pydantic import ValidationError

from app.schemas.device_command_policy import DeviceCommandPolicyCreate


def test_state_changing_command_rejects_asset_type_scope() -> None:
    with pytest.raises(ValidationError, match=r"变更类命令.*scope.*asset"):
        DeviceCommandPolicyCreate(
            scope="asset_type",
            asset_type="switch",
            command_name="reboot",
            decision="whitelist",
        )


def test_state_changing_command_accepts_asset_scope() -> None:
    policy = DeviceCommandPolicyCreate(
        scope="asset",
        asset_id=1,
        command_name="reboot",
        decision="whitelist",
    )
    assert policy.command_name == "reboot"


def test_read_only_command_still_accepts_asset_type_scope() -> None:
    """回归：只读命令不受这条新规则影响。"""
    policy = DeviceCommandPolicyCreate(
        scope="asset_type",
        asset_type="switch",
        command_name="show_version",
        decision="whitelist",
    )
    assert policy.scope == "asset_type"
