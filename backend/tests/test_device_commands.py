"""命令目录：只读、代码层、按厂商区分真实命令字符串。"""

import pytest

from app.agent.device_commands import (
    DEVICE_COMMAND_CATALOG_VERSION,
    UnknownDeviceCommandError,
    UnsupportedVendorError,
    command_supports_vendor,
    command_type_of,
    get_command_template,
    get_device_command,
    list_commands_for_vendor,
    list_device_commands,
    validate_interface_name,
)


def test_catalog_contains_expected_commands() -> None:
    names = {item.name for item in list_device_commands()}
    assert names == {
        "show_version",
        "show_running_config",
        "show_interfaces",
        "ping",
        "reboot",
        "shutdown",
        "port_enable",
        "port_disable",
    }


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


def test_catalog_contains_state_changing_commands() -> None:
    names = {item.name for item in list_device_commands()}
    assert {"reboot", "shutdown", "port_enable", "port_disable"} <= names


def test_state_changing_commands_are_flagged() -> None:
    for name in ("reboot", "shutdown", "port_enable", "port_disable"):
        assert get_device_command(name).command_type == "state_changing"


def test_shutdown_only_supports_linux_generic() -> None:
    """网络设备没有通用整机关机语义，shutdown 只登记 linux/generic。"""
    shutdown = get_device_command("shutdown")
    assert set(shutdown.templates) == {"linux", "generic"}


def test_reboot_has_confirmation_for_network_vendors() -> None:
    reboot = get_device_command("reboot")
    assert reboot.confirmation is not None
    for vendor in ("cisco_iosxe", "huawei_vrp", "hp_comware", "juniper_junos"):
        assert vendor in reboot.confirmation


def test_port_commands_require_interface_argument() -> None:
    for name in ("port_enable", "port_disable"):
        assert get_device_command(name).requires_argument == "interface_name"
    for name in ("show_version", "reboot", "shutdown"):
        assert get_device_command(name).requires_argument == "none"


def test_port_commands_config_templates_exclude_generic_driver_vendors() -> None:
    """hp_comware/linux/generic 未登记配置模式模板，不提供端口启停命令。"""
    port_disable = get_device_command("port_disable")
    assert port_disable.config_templates is not None
    assert set(port_disable.config_templates) == {"cisco_iosxe", "huawei_vrp", "juniper_junos"}
    assert "hp_comware" not in port_disable.config_templates
    assert "linux" not in port_disable.config_templates


def test_list_commands_for_vendor_includes_config_mode_only_commands() -> None:
    """port_enable/port_disable 的 templates={}，但通过 config_templates 支持——发现工具靠这个函数看到它们。"""
    names = {item.name for item in list_commands_for_vendor("cisco_iosxe")}
    assert {"port_enable", "port_disable", "reboot", "show_version"} <= names
    assert command_supports_vendor("port_disable", "cisco_iosxe") is True
    assert command_supports_vendor("port_disable", "hp_comware") is False


def test_junos_port_config_template_includes_explicit_commit() -> None:
    """Junos 是 set/delete + commit 模式，模板必须显式包含 commit。"""
    port_disable = get_device_command("port_disable")
    assert port_disable.config_templates is not None
    assert "commit" in port_disable.config_templates["juniper_junos"]
    port_enable = get_device_command("port_enable")
    assert port_enable.config_templates is not None
    assert "commit" in port_enable.config_templates["juniper_junos"]


def test_command_type_of_returns_risk_level_for_known_commands() -> None:
    assert command_type_of("show_version") == "read_only"
    assert command_type_of("ping") == "read_only"
    assert command_type_of("reboot") == "state_changing"
    assert command_type_of("port_disable") == "state_changing"


def test_command_type_of_returns_none_for_unknown_command() -> None:
    assert command_type_of("drop_table") is None


def test_shutdown_unsupported_on_network_vendors() -> None:
    assert command_supports_vendor("shutdown", "cisco_iosxe") is False


def test_get_command_template_rejects_config_mode_only_commands() -> None:
    """port 命令 templates={}，get_command_template 必须 fail-closed。"""
    with pytest.raises(UnsupportedVendorError):
        get_command_template("port_disable", "cisco_iosxe")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("GigabitEthernet0/1", True),
        ("ge-0/0/1", True),
        ("Ethernet1/0/1", True),
        ("", False),
        ("eth0; rm -rf /", False),
        ("eth0\nreload", False),
        ("eth0 reload", False),
        ("a" * 65, False),
    ],
)
def test_interface_name_validation_is_strict_allowlist(value: str, expected: bool) -> None:
    assert validate_interface_name(value) is expected
