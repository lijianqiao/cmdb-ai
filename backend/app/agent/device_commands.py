"""设备诊断命令目录：唯一能决定"命令字符串到底是什么"的地方。

实现流程：
1. 数据库里的白/黑名单策略（见 app/crud/device_command_policy.py）只决定
   "要不要跳过人工审批"，不能凭空发明新命令——真正会在设备上执行的字符串
   永远来自这个模块，改动这里要走代码 review，不是运行时可配的。
2. 同一个语义命令（比如"看版本"）在不同厂商设备上的真实命令行不一样：
   思科是 show version，华为/H3C 的 VRP/Comware 是 display version。
   DeviceCommandDefinition.templates 按厂商分别登记，厂商没覆盖到就等于
   "这个厂商不支持这个命令"。
3. VendorName 定义在这里而不是 app/schemas/cmdb.py：厂商是否有效，唯一
   权威来源就是这个目录——目录里没有任何命令给这个厂商登记模板，这个厂商
   值本身就没有意义。CmdbAsset 的 vendor 字段校验从这里导入这个类型。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

type VendorName = Literal[
    "cisco_iosxe",
    "huawei_vrp",
    "hp_comware",
    "juniper_junos",
    "linux",
    "generic",
]
type CommandName = Literal[
    "show_version",
    "show_running_config",
    "show_interfaces",
    "ping",
]
type CommandType = Literal["read_only", "state_changing"]

DEVICE_COMMAND_CATALOG_VERSION = "t12-v2"


@dataclass(frozen=True, slots=True)
class DeviceCommandDefinition:
    """一条命令的完整定义：语义 + 按厂商区分的真实命令字符串。"""

    name: CommandName
    version: str
    description: str
    command_type: CommandType
    templates: Mapping[VendorName, str]


class UnknownDeviceCommandError(ValueError):
    """请求的命令名不在目录里，在分配任何资源前就该拒绝。"""


class UnsupportedVendorError(ValueError):
    """命令存在，但目录里没有为这个厂商登记模板——跟"未知命令名"是两种不同原因。"""


_DEVICE_COMMAND_CATALOG: dict[CommandName, DeviceCommandDefinition] = {
    "show_version": DeviceCommandDefinition(
        name="show_version",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="查看设备/系统版本信息",
        command_type="read_only",
        templates={
            "generic": "cat /etc/os-release && uname -a",
            "linux": "cat /etc/os-release && uname -a",
            "cisco_iosxe": "show version",
            "huawei_vrp": "display version",
            "hp_comware": "display version",
            "juniper_junos": "show version",
        },
    ),
    "show_running_config": DeviceCommandDefinition(
        name="show_running_config",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="查看当前生效配置（可能包含敏感信息，建议默认不进白名单）",
        command_type="read_only",
        templates={
            "cisco_iosxe": "show running-config",
            "huawei_vrp": "display current-configuration",
            "hp_comware": "display current-configuration",
            "juniper_junos": "show configuration",
        },
    ),
    "show_interfaces": DeviceCommandDefinition(
        name="show_interfaces",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="查看接口状态",
        command_type="read_only",
        templates={
            "cisco_iosxe": "show interfaces status",
            "huawei_vrp": "display interface brief",
            "hp_comware": "display interface brief",
            "juniper_junos": "show interfaces terse",
        },
    ),
    "ping": DeviceCommandDefinition(
        name="ping",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description=(
            "从设备本机发起连通性测试："
            "Linux/generic 解析本机默认网关；"
            "网络厂商固定探测 1.1.1.1（非用户参数，避免被当探测跳板）"
        ),
        command_type="read_only",
        templates={
            "generic": "ping -c 4 -W 2 $(ip route | awk '/default/ {print $3}')",
            "linux": "ping -c 4 -W 2 $(ip route | awk '/default/ {print $3}')",
            # 网络设备 CLI 无法在单条命令里可靠解析默认网关；v1 用固定公网探测地址，
            # 禁止 <placeholder> 原样下发（见 test_templates_have_no_angle_bracket_placeholders）。
            "cisco_iosxe": "ping 1.1.1.1",
            "huawei_vrp": "ping 1.1.1.1",
            "hp_comware": "ping 1.1.1.1",
            # Junos ping 默认不停止，必须显式 count。
            "juniper_junos": "ping 1.1.1.1 count 4",
        },
    ),
}


def get_device_command(name: str) -> DeviceCommandDefinition:
    """返回目录里的一条命令定义；未知命令名在分配任何资源前失败关闭。"""
    if name not in _DEVICE_COMMAND_CATALOG:
        raise UnknownDeviceCommandError(f"unknown device command {name!r}")
    return _DEVICE_COMMAND_CATALOG[name]


def list_device_commands() -> tuple[DeviceCommandDefinition, ...]:
    """按目录里登记的顺序返回全部命令定义。"""
    return tuple(_DEVICE_COMMAND_CATALOG.values())


def command_supports_vendor(command_name: str, vendor: str) -> bool:
    """命令名未知，或者已知但这个厂商没有登记模板，都返回 False。"""
    if command_name not in _DEVICE_COMMAND_CATALOG:
        return False
    return vendor in _DEVICE_COMMAND_CATALOG[command_name].templates


def list_command_names() -> tuple[str, ...]:
    """按登记顺序返回全部命令名，用于拼可行动的错误提示。"""
    return tuple(_DEVICE_COMMAND_CATALOG)


def list_commands_for_vendor(vendor: str) -> tuple[DeviceCommandDefinition, ...]:
    """返回这个厂商登记过模板的全部命令定义；厂商无覆盖时返回空元组。"""
    return tuple(
        definition
        for definition in _DEVICE_COMMAND_CATALOG.values()
        if vendor in definition.templates
    )


def get_command_template(command_name: str, vendor: str) -> str:
    """返回 (命令名, 厂商) 组合对应的真实命令字符串。

    分两步失败，让调用方能给出精确原因：先确认命令名在目录里（否则
    UnknownDeviceCommandError），再确认这个厂商有登记模板（否则
    UnsupportedVendorError）——不像 command_supports_vendor 那样把两种
    情况都折叠成同一个 False。
    """
    definition = get_device_command(command_name)
    if vendor not in definition.templates:
        raise UnsupportedVendorError(
            f"vendor {vendor!r} has no template for command {command_name!r}"
        )
    return definition.templates[vendor]  # type: ignore[index]
