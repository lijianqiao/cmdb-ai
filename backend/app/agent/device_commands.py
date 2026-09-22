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
4. 「设备回了文本」不等于「命令成功」，判定规则也登记在这里（R3）：
   - DEVICE_ERROR_PATTERNS：按厂商登记的明确报错句式（行首锚定的具体短语），
     配置命令交给 Netmiko 的 error_pattern 逐行检查，普通命令只看输出开头几行；
     绝不用一个宽泛的 error 正则扫整段输出——配置正文里本来就有 logging errors 之类正常内容。
   - CONFIG_SUCCESS_MARKERS：需要明确成功回显的厂商（Junos 必须看到 commit complete）。
   - confirmation：按顺序登记每一轮确认提示与应答；设备问了目录里没登记的问题
     （例如「要不要保存配置」），执行器停下、不替人回答、报告不确定。
   - verify_manually：重启/关机发出后连接会断，拿不到成功证据，结果只能交给人工核实。
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

type VendorName = Literal[
    "cisco_iosxe",
    "cisco_small_business",
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
    "reboot",
    "shutdown",
    "port_enable",
    "port_disable",
]
type CommandType = Literal["read_only", "state_changing"]
type RequiresArgument = Literal["none", "interface_name"]

# t14：加入按厂商的错误识别、Junos 提交成功回显、多轮确认与重启/关机的人工核实语义
DEVICE_COMMAND_CATALOG_VERSION = "t14-v1"

# 命令级正则、按厂商 CLI 语法书写；只用于 send_interactive 匹配确认提示，
# 不接受任何运行时输入，跟 templates 一样是代码层常量。
_INTERFACE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9/.\-]{1,64}$")

# 各厂商 CLI 拒绝命令时的明确报错句式。行首锚定、只认具体短语：
# 这些正则会被 Netmiko 以 re.M 逐条检查配置命令的回显，也会被执行器用来检查
# 普通命令输出的开头几行（报错总是紧跟在命令回显之后）。
_UNIX_ERROR_PATTERN = (
    r"^(?:sudo: .*|.*: (?:command not found|Permission denied|Operation not permitted))$"
)
DEVICE_ERROR_PATTERNS: Mapping[VendorName, str] = {
    "cisco_iosxe": (
        r"^\s*%\s*(?:Invalid input|Incomplete command|Ambiguous command|Unknown command"
        r"|Unrecognized command|(?:Command )?[Aa]uthorization failed)"
    ),
    "cisco_small_business": (
        r"^\s*%\s*(?:Unrecognized command|Invalid input|Incomplete command|Ambiguous command"
        r"|missing mandatory parameter|bad parameter value|Wrong number of parameters"
        r"|Authorization failed)"
    ),
    "huawei_vrp": r"^\s*Error:",
    "hp_comware": (
        r"^\s*%\s*(?:Unrecognized command|Incomplete command|Wrong parameter"
        r"|Too many parameters|Ambiguous command|Permission denied|Authorization failed)"
    ),
    "juniper_junos": r"^\s*(?:syntax error|unknown command|error:)",
    "linux": _UNIX_ERROR_PATTERN,
    "generic": _UNIX_ERROR_PATTERN,
}

# 配置命令需要看到的明确成功回显；没登记的厂商以「逐行无报错」为成功。
CONFIG_SUCCESS_MARKERS: Mapping[VendorName, str] = {
    "juniper_junos": r"commit complete",
}

# 重启确认提示：同一行里要提到 reboot/reload/reset，并带确认记号；
# 含 save 的行（保存配置的询问）一律不匹配——那是另一个决定，不能替人回答。
_REBOOT_CONFIRM_PROMPT = (
    r"(?im)^(?!.*\bsave).*\b(?:reboot|reload|reset)\b.*"
    r"(?:\[confirm\]|\[y/n\]|\(y/n\)|\[yes,no\])"
)


def validate_interface_name(value: str) -> bool:
    """接口名严格白名单校验：只允许字母数字/斜杠/点/短横线，拒绝空白与控制字符。"""
    return bool(_INTERFACE_NAME_PATTERN.fullmatch(value))


@dataclass(frozen=True, slots=True)
class CommandConfirmation:
    """交互式确认提示的匹配正则与应答内容（配 Netmiko send_command_timing 两段式使用）。"""

    prompt_pattern: str
    response: str


@dataclass(frozen=True, slots=True)
class DeviceCommandDefinition:
    """一条命令的完整定义：语义 + 按厂商区分的真实命令字符串。"""

    name: CommandName
    version: str
    description: str
    command_type: CommandType
    templates: Mapping[VendorName, str]
    requires_argument: RequiresArgument = "none"
    # 仅 config-mode 命令（如端口开关）使用；send_configs 而非 send_command 执行。
    config_templates: Mapping[VendorName, tuple[str, ...]] | None = None
    # 仅需要人工确认提示的 exec-mode 命令使用：按顺序登记每一轮提示与应答。
    confirmation: Mapping[VendorName, tuple[CommandConfirmation, ...]] | None = None
    # 发出后拿不到成功证据（重启/关机会断开连接）：结果交给人工核实，不标成已执行。
    verify_manually: bool = False


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
            "cisco_small_business": "show version",
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
            "cisco_small_business": "show running-config",
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
            "cisco_small_business": "show interfaces status",
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
            "cisco_small_business": "ping ip 1.1.1.1",
            "huawei_vrp": "ping 1.1.1.1",
            "hp_comware": "ping 1.1.1.1",
            # Junos ping 默认不停止，必须显式 count。
            "juniper_junos": "ping 1.1.1.1 count 4",
        },
    ),
    "reboot": DeviceCommandDefinition(
        name="reboot",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="重启设备（网络设备走 reload 语义）；执行前会等待设备确认提示",
        command_type="state_changing",
        templates={
            "generic": "sudo reboot",
            "linux": "sudo reboot",
            "cisco_iosxe": "reload",
            "cisco_small_business": "reload",
            "huawei_vrp": "reboot",
            "hp_comware": "reboot",
            "juniper_junos": "request system reboot",
        },
        confirmation={
            "cisco_iosxe": (CommandConfirmation(prompt_pattern=_REBOOT_CONFIRM_PROMPT, response="\n"),),
            "cisco_small_business": (
                CommandConfirmation(prompt_pattern=_REBOOT_CONFIRM_PROMPT, response="y"),
            ),
            "huawei_vrp": (CommandConfirmation(prompt_pattern=_REBOOT_CONFIRM_PROMPT, response="y"),),
            "hp_comware": (CommandConfirmation(prompt_pattern=_REBOOT_CONFIRM_PROMPT, response="y"),),
            "juniper_junos": (
                CommandConfirmation(prompt_pattern=_REBOOT_CONFIRM_PROMPT, response="yes"),
            ),
        },
        verify_manually=True,
    ),
    "shutdown": DeviceCommandDefinition(
        name="shutdown",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description=(
            "关闭设备电源；仅 Linux/generic 主机有意义（网络设备没有通用整机断电 CLI，"
            "调用会按厂商不支持 fail-closed，不会被当成重启执行）"
        ),
        command_type="state_changing",
        templates={
            "generic": "sudo shutdown -h now",
            "linux": "sudo shutdown -h now",
        },
        verify_manually=True,
    ),
    "port_enable": DeviceCommandDefinition(
        name="port_enable",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="启用一个网络接口（no shutdown / undo shutdown 语义）",
        command_type="state_changing",
        templates={},
        requires_argument="interface_name",
        config_templates={
            "cisco_iosxe": ("interface {interface}", "no shutdown"),
            "cisco_small_business": ("interface {interface}", "no shutdown"),
            "huawei_vrp": ("interface {interface}", "undo shutdown"),
            "juniper_junos": ("delete interfaces {interface} disable", "commit"),
        },
    ),
    "port_disable": DeviceCommandDefinition(
        name="port_disable",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="禁用一个网络接口（shutdown 语义）",
        command_type="state_changing",
        templates={},
        requires_argument="interface_name",
        config_templates={
            "cisco_iosxe": ("interface {interface}", "shutdown"),
            "cisco_small_business": ("interface {interface}", "shutdown"),
            "huawei_vrp": ("interface {interface}", "shutdown"),
            "juniper_junos": ("set interfaces {interface} disable", "commit"),
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
    """命令名未知，或者两种登记方式（exec 模板 / config 模板）都没有这个厂商，才算不支持。"""
    definition = _DEVICE_COMMAND_CATALOG.get(command_name)  # type: ignore[call-overload]
    if definition is None:
        return False
    if vendor in definition.templates:
        return True
    return definition.config_templates is not None and vendor in definition.config_templates


def list_command_names() -> tuple[str, ...]:
    """按登记顺序返回全部命令名，用于拼可行动的错误提示。"""
    return tuple(_DEVICE_COMMAND_CATALOG)


def list_commands_for_vendor(vendor: str) -> tuple[DeviceCommandDefinition, ...]:
    """返回这个厂商能以任意方式（exec 或 config 模式）执行的全部命令定义。"""
    return tuple(
        definition for definition in _DEVICE_COMMAND_CATALOG.values()
        if command_supports_vendor(definition.name, vendor)
    )


def command_type_of(command_name: str) -> CommandType | None:
    """返回命令的风险分级；命令名未知时返回 None（调用方自行决定如何处理）。"""
    definition = _DEVICE_COMMAND_CATALOG.get(command_name)  # type: ignore[call-overload]
    return definition.command_type if definition else None


def rendered_command_lines(
    command_name: str, vendor: str, *, interface_name: str | None
) -> tuple[str, ...]:
    """返回这条命令在该厂商上实际下发的命令行：config 模式按接口名填好模板，exec 模式一行。

    执行器和审批证据快照都用它，保证证据里记的就是真正发出去的内容。
    """
    definition = get_device_command(command_name)
    if definition.config_templates is not None and vendor in definition.config_templates:
        return tuple(
            line.format(interface=interface_name)
            for line in definition.config_templates[vendor]  # type: ignore[index]
        )
    return (get_command_template(command_name, vendor),)


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
