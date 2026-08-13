"""命令目录：只读、代码层、按厂商区分真实命令字符串。"""

import pytest

from app.agent.device_commands import (
    DEVICE_COMMAND_CATALOG_VERSION,
    UnknownDeviceCommandError,
    UnsupportedVendorError,
    command_supports_vendor,
    get_command_template,
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
    assert command_supports_vendor("show_running_config", "linux") is False


def test_command_supports_vendor_returns_false_for_unknown_command() -> None:
    assert command_supports_vendor("drop_table", "cisco_iosxe") is False


def test_catalog_is_immutable() -> None:
    definition = get_device_command("show_version")
    with pytest.raises(AttributeError):
        definition.name = "hacked"  # type: ignore[misc]


def test_templates_have_no_angle_bracket_placeholders() -> None:
    """目录模板必须是可直接下发的命令字符串，禁止遗留 <gateway> 这类未替换占位符。"""
    for item in list_device_commands():
        for vendor, template in item.templates.items():
            assert "<" not in template and ">" not in template, (
                f"{item.name}/{vendor} 模板含尖括号占位符: {template!r}"
            )


def test_network_vendor_ping_uses_fixed_probe_target() -> None:
    ping = get_device_command("ping")
    assert ping.templates["cisco_iosxe"] == "ping 1.1.1.1"
    assert ping.templates["huawei_vrp"] == "ping 1.1.1.1"
    assert ping.templates["hp_comware"] == "ping 1.1.1.1"
    assert ping.templates["juniper_junos"] == "ping 1.1.1.1 count 4"


def test_get_command_template_returns_real_string_for_supported_vendor() -> None:
    assert get_command_template("show_running_config", "cisco_iosxe") == "show running-config"


def test_get_command_template_raises_unknown_command_error_for_unknown_name() -> None:
    """命令名根本不在目录里——跟"厂商不支持"是两种不同原因，调用方要能分辨。"""
    with pytest.raises(UnknownDeviceCommandError):
        get_command_template("drop_table", "cisco_iosxe")


def test_get_command_template_raises_unsupported_vendor_error_for_known_command() -> None:
    """命令存在，但目录里没给这个厂商登记模板——不能跟"未知命令名"报同一个错。"""
    with pytest.raises(UnsupportedVendorError):
        get_command_template("show_running_config", "linux")
