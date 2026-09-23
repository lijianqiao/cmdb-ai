"""命令目录：只读、代码层、按厂商区分真实命令字符串。"""

import re

import pytest

from app.agent.device_commands import (
    DEVICE_COMMAND_CATALOG_VERSION,
    DEVICE_ERROR_PATTERNS,
    UnknownDeviceCommandError,
    UnsupportedVendorError,
    command_supports_vendor,
    command_type_of,
    filter_exact_ip_lines,
    get_command_template,
    get_device_command,
    list_commands_for_vendor,
    list_device_commands,
    normalize_command_arguments,
    normalize_interface_names,
    rendered_command_lines,
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
        "port_enable",
        "port_disable",
        *TROUBLESHOOTING_COMMANDS,
    }


# P2b-2a：输出有限、按对象定位的排查命令。只登记华为和 H3C（主力设备），思科等暂不支持。
TROUBLESHOOTING_COMMANDS = {
    # 设备健康
    "show_cpu",
    "show_memory",
    "show_environment",
    "show_stack",
    "show_clock",
    "show_ntp",
    # 接口与链路
    "show_interfaces_description",
    "show_interface_detail",
    "show_interface_config",
    "show_down_interfaces",
    "show_error_down",
    "show_interface_errors",
    "show_interface_traffic",
    "show_transceiver",
    "show_port_channel",
    "show_poe",
    # 二层
    "show_vlan",
    "show_vlan_detail",
    "show_mac_lookup",
    "show_mac_on_interface",
    "show_mac_in_vlan",
    "show_mac_flapping",
    "show_stp",
    "show_stp_root",
    "show_stp_tc",
    "show_lldp_neighbors",
    # 三层与连通性
    "show_ip_interfaces",
    "show_arp_lookup",
    "show_route_lookup",
    "show_default_route",
    "show_vrrp",
    "show_ospf_neighbors",
    "show_bgp_summary",
    "show_dhcp_snooping_bindings",
}

# 某个主力厂商确实没有等价命令的，在这里登记；其余排查命令两家都必须有模板。
_KNOWN_VENDOR_GAPS = {"show_error_down": {"hp_comware"}}


def test_troubleshooting_commands_cover_both_main_vendors() -> None:
    """排查命令是给华为 / H3C 现网用的：漏登记一家，模型在那家设备上就查不了。"""
    for name in TROUBLESHOOTING_COMMANDS:
        definition = get_device_command(name)
        assert definition.command_type == "read_only", name
        missing = {"huawei_vrp", "hp_comware"} - set(definition.templates)
        assert missing == _KNOWN_VENDOR_GAPS.get(name, set()), (name, missing)


def test_troubleshooting_commands_are_not_registered_for_unverified_vendors() -> None:
    """没在真机上核对过的厂商不登记：写错的模板会在现网报错，还不如明确「不支持」。"""
    for name in TROUBLESHOOTING_COMMANDS:
        vendors = set(get_device_command(name).templates)
        assert vendors <= {"huawei_vrp", "hp_comware"}, (name, vendors)


def test_host_vendors_and_power_off_command_are_retired() -> None:
    """命令只面向网络设备：整机关机只对 Linux 主机有意义，随主机厂商一起下线。"""
    with pytest.raises(UnknownDeviceCommandError):
        get_device_command("shutdown")
    for item in list_device_commands():
        vendors = set(item.templates) | set(item.config_templates or {})
        assert not vendors & {"linux", "generic"}, item.name


def test_other_vendor_has_no_commands() -> None:
    """other 是暂不支持的网络设备厂商的占位值：能登记进 CMDB，但任何命令都不支持。"""
    assert list_commands_for_vendor("other") == ()
    for item in list_device_commands():
        assert command_supports_vendor(item.name, "other") is False


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
    assert command_supports_vendor("show_running_config", "other") is False


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


def test_every_template_placeholder_is_a_registered_argument() -> None:
    """模板里出现的占位符必须是这条命令登记过的参数，否则渲染时会抛 KeyError。

    P2b 按厂商往目录里贴模板，最容易犯的错就是「模板写了 {vlan}、参数忘了登记」。
    """
    allowed = {
        "interface_name": "interface",
        "interface_names": "interface",
        "ip_address": "ip",
        "mac_address": "mac",
        "vlan_id": "vlan",
    }
    for item in list_device_commands():
        expected = {allowed[name] for name in item.arguments}
        for vendor, template in [
            *item.templates.items(),
            *(
                (vendor, line)
                for vendor, lines in (item.config_templates or {}).items()
                for line in lines
            ),
        ]:
            used = set(re.findall(r"\{(\w+)\}", template))
            assert used <= expected, f"{item.name}/{vendor} 用了没登记的占位符: {used - expected}"


def test_get_command_template_returns_real_string_for_supported_vendor() -> None:
    assert get_command_template("show_running_config", "cisco_iosxe") == "show running-config"


def test_get_command_template_raises_unknown_command_error_for_unknown_name() -> None:
    """命令名根本不在目录里——跟"厂商不支持"是两种不同原因，调用方要能分辨。"""
    with pytest.raises(UnknownDeviceCommandError):
        get_command_template("drop_table", "cisco_iosxe")


def test_get_command_template_raises_unsupported_vendor_error_for_known_command() -> None:
    """命令存在，但目录里没给这个厂商登记模板——不能跟"未知命令名"报同一个错。"""
    with pytest.raises(UnsupportedVendorError):
        get_command_template("show_running_config", "other")


def test_catalog_contains_state_changing_commands() -> None:
    names = {item.name for item in list_device_commands()}
    assert {"reboot", "port_enable", "port_disable"} <= names


def test_state_changing_commands_are_flagged() -> None:
    for name in ("reboot", "port_enable", "port_disable"):
        assert get_device_command(name).command_type == "state_changing"


def test_reboot_has_confirmation_for_network_vendors() -> None:
    reboot = get_device_command("reboot")
    assert reboot.confirmation is not None
    for vendor in (
        "cisco_iosxe",
        "cisco_small_business",
        "huawei_vrp",
        "hp_comware",
        "juniper_junos",
    ):
        assert vendor in reboot.confirmation


def test_port_commands_take_interface_names_argument() -> None:
    for name in ("port_enable", "port_disable"):
        assert get_device_command(name).arguments == ("interface_names",)
    for name in ("show_version", "reboot"):
        assert get_device_command(name).arguments == ()


def test_batch_expands_config_template_for_each_interface() -> None:
    """一条提案一组接口：逐口展开现有模板，不用各厂商的 range 语法。"""
    lines = rendered_command_lines(
        "port_disable",
        "cisco_iosxe",
        arguments={"interface_names": ["Gi1/0/15", "Gi1/0/16", "Gi1/0/17"]},
    )
    assert lines == (
        "interface Gi1/0/15",
        "shutdown",
        "interface Gi1/0/16",
        "shutdown",
        "interface Gi1/0/17",
        "shutdown",
    )


def test_junos_batch_commits_once_at_the_end() -> None:
    """Junos 是 set/delete + commit：一批接口只在最后提交一次。"""
    lines = rendered_command_lines(
        "port_disable",
        "juniper_junos",
        arguments={"interface_names": ["ge-0/0/1", "ge-0/0/2", "ge-0/0/3"]},
    )
    assert lines == (
        "set interfaces ge-0/0/1 disable",
        "set interfaces ge-0/0/2 disable",
        "set interfaces ge-0/0/3 disable",
        "commit",
    )


def test_normalize_interface_names_dedupes_keeping_order() -> None:
    assert normalize_interface_names(["Gi1/0/16", "Gi1/0/15", "Gi1/0/16"]) == (
        "Gi1/0/16",
        "Gi1/0/15",
    )


def test_normalize_interface_names_accepts_48_interfaces() -> None:
    names = [f"Gi1/0/{index}" for index in range(1, 49)]
    assert len(normalize_interface_names(names)) == 48


@pytest.mark.parametrize(
    "values",
    [
        [],
        ["eth0; reload"],
        ["Gi1/0/1", "eth0 reload"],
        [f"Gi1/0/{index}" for index in range(1, 50)],
    ],
)
def test_normalize_interface_names_rejects_invalid_lists(values: list[str]) -> None:
    """空列表、任一非法接口名、超过 48 个接口，都整体拒绝。"""
    with pytest.raises(ValueError):
        normalize_interface_names(values)


def test_port_commands_config_templates_cover_all_network_vendors() -> None:
    """五个网络厂商都登记端口启停，包括 H3C Comware。"""
    port_disable = get_device_command("port_disable")
    port_enable = get_device_command("port_enable")
    assert port_disable.config_templates is not None
    assert port_enable.config_templates is not None
    assert set(port_disable.config_templates) == set(port_enable.config_templates)
    assert set(port_disable.config_templates) == {
        "cisco_iosxe",
        "cisco_small_business",
        "huawei_vrp",
        "hp_comware",
        "juniper_junos",
    }
    assert port_enable.config_templates["hp_comware"] == (
        "interface {interface}",
        "undo shutdown",
    )
    assert port_disable.config_templates["hp_comware"] == (
        "interface {interface}",
        "shutdown",
    )


def test_list_commands_for_vendor_includes_config_mode_only_commands() -> None:
    """port_enable/port_disable 的 templates={}，但通过 config_templates 支持——发现工具靠这个函数看到它们。"""
    names = {item.name for item in list_commands_for_vendor("cisco_iosxe")}
    assert {"port_enable", "port_disable", "reboot", "show_version"} <= names
    assert command_supports_vendor("port_disable", "cisco_iosxe") is True
    assert command_supports_vendor("port_enable", "hp_comware") is True
    assert command_supports_vendor("port_disable", "hp_comware") is True
    assert command_supports_vendor("port_enable", "other") is False


def test_junos_port_enable_deletes_disable_and_commits() -> None:
    """Junos 的开端口是删掉 disable 再提交，commit 由厂商级规则追加一次。"""
    assert rendered_command_lines(
        "port_enable", "juniper_junos", arguments={"interface_names": ["ge-0/0/1"]}
    ) == ("delete interfaces ge-0/0/1 disable", "commit")


def test_normalize_command_arguments_enforces_what_the_catalog_registered() -> None:
    """命令能接哪些参数只由目录说了算：没登记的拒绝，登记的必须给且合法。"""
    with pytest.raises(ValueError, match="不接受 interface_names"):
        normalize_command_arguments("reboot", {"interface_names": ["Gi1/0/1"]})
    with pytest.raises(ValueError, match="合法的接口名"):
        normalize_command_arguments("port_disable", {})
    assert normalize_command_arguments(
        "port_disable", {"interface_names": ["Gi1/0/2", "Gi1/0/1", "Gi1/0/2"]}
    ) == {"interface_names": ["Gi1/0/2", "Gi1/0/1"]}


def test_single_interface_argument_is_validated_and_rendered(
    single_interface_read_only_command: str,
) -> None:
    """只读命令的单接口参数走同一条链路：P2b 加这类命令时不用再改代码。"""
    command = single_interface_read_only_command

    assert normalize_command_arguments(command, {"interface_name": "Gi1/0/15"}) == {
        "interface_name": "Gi1/0/15"
    }
    assert rendered_command_lines(
        command, "cisco_iosxe", arguments={"interface_name": "Gi1/0/15"}
    ) == ("show interfaces Gi1/0/15",)
    with pytest.raises(ValueError, match="接口名"):
        normalize_command_arguments(command, {"interface_name": "eth0; reload"})
    # 两种接口参数不混用：登记的是单个接口时，给列表也要拒绝。
    with pytest.raises(ValueError, match="不接受 interface_names"):
        normalize_command_arguments(
            command, {"interface_name": "Gi1/0/15", "interface_names": ["Gi1/0/15"]}
        )


def test_commands_without_arguments_render_the_template_as_is() -> None:
    assert rendered_command_lines("show_version", "cisco_iosxe", arguments=None) == (
        "show version",
    )


def test_command_type_of_returns_risk_level_for_known_commands() -> None:
    assert command_type_of("show_version") == "read_only"
    assert command_type_of("ping") == "read_only"
    assert command_type_of("reboot") == "state_changing"
    assert command_type_of("port_disable") == "state_changing"


def test_command_type_of_returns_none_for_unknown_command() -> None:
    assert command_type_of("drop_table") is None


def test_get_command_template_rejects_config_mode_only_commands() -> None:
    """port 命令 templates={}，get_command_template 必须 fail-closed。"""
    with pytest.raises(UnsupportedVendorError):
        get_command_template("port_disable", "cisco_iosxe")


def test_cisco_small_business_uses_sg350x_commands() -> None:
    assert get_command_template("show_version", "cisco_small_business") == "show version"
    assert (
        get_command_template("show_running_config", "cisco_small_business")
        == "show running-config"
    )
    assert (
        get_command_template("show_interfaces", "cisco_small_business")
        == "show interfaces status"
    )
    # SG350X 的 ping 要带 ip 关键字，和 IOS-XE 不一样，所以单独核一遍渲染结果。
    assert rendered_command_lines(
        "ping", "cisco_small_business", arguments={"ip_address": "10.1.2.3"}
    ) == ("ping ip 10.1.2.3",)

    reboot = get_device_command("reboot")
    assert reboot.templates["cisco_small_business"] == "reload"
    assert reboot.confirmation is not None
    (confirmation,) = reboot.confirmation["cisco_small_business"]
    assert confirmation.response == "y"

    port_enable = get_device_command("port_enable")
    assert port_enable.config_templates is not None
    assert port_enable.config_templates["cisco_small_business"] == (
        "interface {interface}",
        "no shutdown",
    )
    port_disable = get_device_command("port_disable")
    assert port_disable.config_templates is not None
    assert port_disable.config_templates["cisco_small_business"] == (
        "interface {interface}",
        "shutdown",
    )


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


# R3：确认流程与错误识别都登记在目录里，执行器只照目录办事。


@pytest.mark.parametrize(
    ("vendor", "prompt"),
    [
        ("cisco_iosxe", "Proceed with reload? [confirm]"),
        (
            "cisco_small_business",
            "This command will reset the whole system and disconnect your current session. "
            "Do you want to continue ? (Y/N)[N]",
        ),
        ("huawei_vrp", "System will reboot! Continue? [y/n]:"),
        ("hp_comware", "This command will reboot the device. Continue? [Y/N]:"),
        ("juniper_junos", "Reboot the system ? [yes,no] (no)"),
    ],
)
def test_reboot_confirmation_matches_the_reboot_question(vendor: str, prompt: str) -> None:
    reboot = get_device_command("reboot")
    assert reboot.confirmation is not None
    (step,) = reboot.confirmation[vendor]
    assert re.search(step.prompt_pattern, prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "System configuration has been modified. Save? [yes/no]:",
        "You haven't saved your changes. Are you sure you want to continue ? (Y/N)[N]",
        "Warning: All the configuration will be saved to the next startup configuration. "
        "Continue? [y/n]:",
        "Current configuration will be lost after the reboot, save current configuration? [Y/N]:",
    ],
)
def test_reboot_confirmation_never_answers_a_save_configuration_question(prompt: str) -> None:
    """「要不要保存配置」是另一个决定：替人回答 y 会悄悄存盘或丢弃改动。
    目录里的重启确认一个都不能匹配它——匹配不上执行器就停下、报告不确定。"""
    reboot = get_device_command("reboot")
    assert reboot.confirmation is not None
    for steps in reboot.confirmation.values():
        for step in steps:
            assert not re.search(step.prompt_pattern, prompt), (step.prompt_pattern, prompt)


def test_every_vendor_has_a_device_error_pattern() -> None:
    vendors = {vendor for item in list_device_commands() for vendor in item.templates}
    assert vendors <= set(DEVICE_ERROR_PATTERNS)


def test_only_reboot_needs_manual_verification() -> None:
    """重启后连接会断：拿不到成功证据，结果只能交给人工核实。"""
    needs_manual = {item.name for item in list_device_commands() if item.verify_manually}
    assert needs_manual == {"reboot"}


def test_ping_probes_a_caller_given_target_instead_of_a_fixed_address() -> None:
    """ping 的目标由调用方给：固定探 1.1.1.1 查不了「这台设备能不能通业务网关」。"""
    assert normalize_command_arguments("ping", {"ip_address": "10.1.2.3"}) == {
        "ip_address": "10.1.2.3"
    }
    assert rendered_command_lines(
        "ping", "cisco_iosxe", arguments={"ip_address": "10.1.2.3"}
    ) == ("ping 10.1.2.3 repeat 4",)
    assert rendered_command_lines(
        "ping", "huawei_vrp", arguments={"ip_address": "10.1.2.3"}
    ) == ("ping -c 4 10.1.2.3",)
    # 目标是必填的：不给就不该拼出一条没有目标的命令发下去。
    with pytest.raises(ValueError, match="目标 IP"):
        normalize_command_arguments("ping", {})
    with pytest.raises(ValueError, match="不接受 interface_name"):
        normalize_command_arguments("ping", {"ip_address": "10.1.2.3", "interface_name": "Gi1/0/1"})


@pytest.mark.parametrize(
    "value",
    [
        "10.1.2.3; reload",  # 命令拼接
        "224.0.0.1",  # 组播
        "255.255.255.255",  # 广播
        "0.0.0.0",  # 未指定地址
        "10.1.2.300",  # 不是合法 IP
        "sw-01",  # 主机名不接受：目录只发 IP，域名解析交给人
        "",
    ],
)
def test_ip_address_argument_rejects_unusable_targets(value: str) -> None:
    """目标地址进模板前必须是一个能真正探测的单播 IP。"""
    with pytest.raises(ValueError, match="目标 IP"):
        normalize_command_arguments("ping", {"ip_address": value})


def test_ip_address_argument_accepts_ipv6() -> None:
    assert normalize_command_arguments("ping", {"ip_address": "2001:db8::1"}) == {
        "ip_address": "2001:db8::1"
    }


def test_slow_commands_are_registered_in_the_long_read_timeout_class() -> None:
    """整份配置这类慢命令不能和 show_version 共用 60 秒读超时，否则大配置必超时。"""
    assert get_device_command("show_running_config").read_timeout_class == "long"
    assert get_device_command("show_version").read_timeout_class == "short"
    assert get_device_command("ping").read_timeout_class == "short"


def test_only_commands_that_send_packets_to_a_target_are_marked_as_probing() -> None:
    """D2 靠这个标记决定「目标不在 CMDB 就转人工」，不在 hitl.py 里写死命令名。

    show_arp_lookup / show_route_lookup 也带 ip_address，但只查设备本地的表，不发包。
    """
    probing = {item.name for item in list_device_commands() if item.probes_target}
    assert probing == {"ping"}


@pytest.mark.parametrize(
    "value",
    ["aabb.ccdd.eeff", "AABB-CCDD-EEFF", "aa:bb:cc:dd:ee:ff", "aa-bb-cc-dd-ee-ff"],
)
def test_mac_address_accepts_common_notations_and_stores_one_canonical_form(value: str) -> None:
    """用户和模型会用各种写法：统一存成 12 位小写十六进制，渲染时再按厂商格式化。

    载荷里存厂商无关的形式，资产换了厂商之后重试也能渲染对。
    """
    assert normalize_command_arguments("show_mac_lookup", {"mac_address": value}) == {
        "mac_address": "aabbccddeeff"
    }


@pytest.mark.parametrize(
    "value",
    ["aabb.ccdd.eef", "gg:bb:cc:dd:ee:ff", "aa:bb-cc:dd-ee:ff", "aabbccddeeff; reboot", ""],
)
def test_mac_address_rejects_malformed_values(value: str) -> None:
    with pytest.raises(ValueError, match="MAC"):
        normalize_command_arguments("show_mac_lookup", {"mac_address": value})


def test_mac_address_is_rendered_in_each_vendors_own_notation() -> None:
    """华为 / H3C 的 CLI 只认 aabb-ccdd-eeff，写成冒号格式会直接报参数错。"""
    arguments = normalize_command_arguments("show_mac_lookup", {"mac_address": "aa:bb:cc:dd:ee:ff"})
    assert rendered_command_lines("show_mac_lookup", "huawei_vrp", arguments=arguments) == (
        "display mac-address aabb-ccdd-eeff",
    )
    assert rendered_command_lines("show_mac_lookup", "hp_comware", arguments=arguments) == (
        "display mac-address aabb-ccdd-eeff",
    )


def test_every_vendor_with_a_mac_template_has_a_mac_notation() -> None:
    """给新厂商登记带 {mac} 的模板时，必须同时登记这个厂商的 MAC 写法，否则渲染时才报错。"""
    arguments = {"mac_address": "aabbccddeeff"}
    for item in list_device_commands():
        for vendor, template in item.templates.items():
            if "{mac}" in template:
                rendered_command_lines(item.name, vendor, arguments=arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [1, 10, 4094])
def test_vlan_id_accepts_the_usable_range(value: int) -> None:
    assert normalize_command_arguments("show_vlan_detail", {"vlan_id": value}) == {
        "vlan_id": value
    }
    assert rendered_command_lines(
        "show_vlan_detail", "hp_comware", arguments={"vlan_id": value}
    ) == (f"display vlan {value}",)


@pytest.mark.parametrize("value", [0, 4095, -1, "10", True, 10.0])
def test_vlan_id_rejects_values_outside_1_to_4094(value: object) -> None:
    """True 也是 int 的子类，不拦的话会渲染成 display vlan True。"""
    with pytest.raises(ValueError, match="VLAN"):
        normalize_command_arguments("show_vlan_detail", {"vlan_id": value})


def test_arp_lookup_output_keeps_only_the_exact_ip() -> None:
    """华为的 display arp | include 10.1.1.1 是子串匹配，会带出 10.1.1.10～19。

    回答「这个 IP 接在哪个口」时混进别的 IP 就是错答，所以执行器要再按整个地址过滤一次。
    """
    output = "\n".join(
        [
            "display arp | include 10.1.1.1",
            "10.1.1.1        aabb-ccdd-0001  20   D-0  GE0/0/1  10",
            "10.1.1.10       aabb-ccdd-0010  20   D-0  GE0/0/2  10",
            "10.1.1.100      aabb-ccdd-0100  20   D-0  GE0/0/3  10",
            "110.1.1.1       aabb-ccdd-0111  20   D-0  GE0/0/4  10",
        ]
    )

    filtered = filter_exact_ip_lines(output, "10.1.1.1")

    assert filtered.splitlines() == [
        "display arp | include 10.1.1.1",
        "10.1.1.1        aabb-ccdd-0001  20   D-0  GE0/0/1  10",
    ]


def test_arp_lookup_is_the_only_command_that_filters_by_exact_ip() -> None:
    assert {item.name for item in list_device_commands() if item.exact_ip_filter} == {
        "show_arp_lookup"
    }
