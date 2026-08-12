"""命令目录：只读、代码层、按厂商区分真实命令字符串。"""

import pytest

from app.agent.device_commands import (
    DEVICE_COMMAND_CATALOG_VERSION,
    UnknownDeviceCommandError,
    command_supports_vendor,
    get_device_command,
    list_device_commands,
)


def test_catalog_contains_expected_commands() -> None:
    names = {item.name for item in list_device_commands()}
    assert names == {"show_version", "show_running_config", "show_interfaces", "ping"}


def test_every_command_is_versioned_and_has_description() -> None:
    for item in list_device_commands():
        assert item.version == DEVICE_COMMAND_CATALOG_VERSION
        assert len(item.description) >= 4
        assert item.command_type in ("read_only", "state_changing")


def test_get_unknown_command_fails_closed() -> None:
    with pytest.raises(UnknownDeviceCommandError):
        get_device_command("drop_table")


def test_show_version_has_templates_for_multiple_vendors() -> None:
    definition = get_device_command("show_version")
    assert definition.templates["cisco_iosxe"] == "show version"
    assert definition.templates["huawei_vrp"] == "display version"
    assert "hp_comware" in definition.templates


def test_command_supports_vendor_reflects_template_presence() -> None:
    assert command_supports_vendor("show_version", "cisco_iosxe") is True
    assert command_supports_vendor("ping", "juniper_junos") is False


def test_command_supports_vendor_returns_false_for_unknown_command() -> None:
    assert command_supports_vendor("drop_table", "cisco_iosxe") is False


def test_catalog_is_immutable() -> None:
    definition = get_device_command("show_version")
    with pytest.raises(AttributeError):
        definition.name = "hacked"  # type: ignore[misc]
