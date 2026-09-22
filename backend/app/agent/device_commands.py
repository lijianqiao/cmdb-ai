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
   唯一的例外是 other：暂不支持的网络设备厂商先以它登记，能进 CMDB 台账和
   依赖图，但故意不登记任何模板，所有命令都按「厂商不支持」拒绝。项目只面向
   网络设备，Linux 等主机厂商和只对主机有意义的整机关机命令已经下线。
4. 「设备回了文本」不等于「命令成功」，判定规则也登记在这里（R3）：
   - DEVICE_ERROR_PATTERNS：按厂商登记的明确报错句式（行首锚定的具体短语），
     配置命令交给 Netmiko 的 error_pattern 逐行检查，普通命令只看输出开头几行；
     绝不用一个宽泛的 error 正则扫整段输出——配置正文里本来就有 logging errors 之类正常内容。
   - CONFIG_SUCCESS_MARKERS：需要明确成功回显的厂商（Junos 必须看到 commit complete）。
   - confirmation：按顺序登记每一轮确认提示与应答；设备问了目录里没登记的问题
     （例如「要不要保存配置」），执行器停下、不替人回答、报告不确定。
   - verify_manually：重启发出后连接会断，拿不到成功证据，结果只能交给人工核实。
5. 端口类命令一次接一组接口（interface_names，最多 48 个）。config_templates 只写
   一个接口的那几行，按接口逐个展开，不用各厂商的 range 语法：四家写法各不相同，
   range 里某个口报错时也分不清是哪一个。进配置模式的命令（CONFIG_MODE_COMMANDS）
   和提交命令（CONFIG_COMMIT_COMMANDS）是厂商级规则：Junos 进私有候选配置
   configure private，中途失败时改动随会话丢弃，不会留在所有人共享的候选配置里；
   整批只在最后 commit 一次。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict, get_args

type VendorName = Literal[
    "cisco_iosxe",
    "cisco_small_business",
    "huawei_vrp",
    "hp_comware",
    "juniper_junos",
    "other",
]
type CommandName = Literal[
    "show_version",
    "show_running_config",
    "show_interfaces",
    "ping",
    "reboot",
    "port_enable",
    "port_disable",
]
type CommandType = Literal["read_only", "state_changing"]
# 命令可以登记的参数名。interface_name 给需要单个接口的只读命令用（4.2 的
# show_interface_detail 等），interface_names 只给端口启停用，两者不混用。
type ArgName = Literal["interface_name", "interface_names"]


class CommandArguments(TypedDict, total=False):
    """一条命令的参数值：键只能是目录登记过的参数名，值已经校验并归一化。"""

    interface_name: str
    interface_names: list[str]

# t15：H3C Comware 补上端口启停。配置在 system-view 里立即生效，和华为一样不自动保存。
# t16：只面向网络设备——下线 linux/generic 厂商和只对主机有意义的 shutdown，新增占位厂商 other。
# t17：端口启停一次接一组接口；Junos 进私有候选配置，commit 改为整批最后提交一次。
DEVICE_COMMAND_CATALOG_VERSION = "t17-v1"

# 一条提案最多带的接口数：一台接入交换机的口数，超过要求分两次。
MAX_INTERFACES_PER_PROPOSAL = 48

# 校验参数时按这个顺序检查，报错信息也按这个顺序给，输出稳定好测。
_ARG_NAMES: tuple[ArgName, ...] = ("interface_name", "interface_names")
_ARG_HINTS: Mapping[ArgName, str] = {"interface_name": "", "interface_names": "列表"}
_INTERFACE_NAME_HINT = "接口名只能包含字母、数字、/、.、-，且不超过 64 个字符"

# 命令级正则、按厂商 CLI 语法书写；只用于 send_interactive 匹配确认提示，
# 不接受任何运行时输入，跟 templates 一样是代码层常量。
_INTERFACE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9/.\-]{1,64}$")

# 各厂商 CLI 拒绝命令时的明确报错句式。行首锚定、只认具体短语：
# 这些正则会被 Netmiko 以 re.M 逐条检查配置命令的回显，也会被执行器用来检查
# 普通命令输出的开头几行（报错总是紧跟在命令回显之后）。
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
}

# 配置命令需要看到的明确成功回显；没登记的厂商以「逐行无报错」为成功。
CONFIG_SUCCESS_MARKERS: Mapping[VendorName, str] = {
    "juniper_junos": r"commit complete",
}

# 进配置模式的命令；没登记的厂商用 Netmiko 驱动的默认命令。Junos 进私有候选配置：
# 共享候选库里有别人未提交的改动时它会拒绝进入，那时一条配置都没发，可以直接重试。
CONFIG_MODE_COMMANDS: Mapping[VendorName, str] = {
    "juniper_junos": "configure private",
}

# 整批配置发完后追加一次的提交命令：Junos 的配置要 commit 才生效。
CONFIG_COMMIT_COMMANDS: Mapping[VendorName, tuple[str, ...]] = {
    "juniper_junos": ("commit",),
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


def normalize_interface_names(values: Sequence[str]) -> tuple[str, ...]:
    """接口列表去重保序并逐个校验：1–48 个，每个都要过接口名白名单。

    不合法时抛 ValueError；原因里不带输入值，可以直接转给模型让它自己改。
    """
    unique = tuple(dict.fromkeys(values))
    if not unique:
        raise ValueError("interface_names 至少要有一个接口")
    if len(unique) > MAX_INTERFACES_PER_PROPOSAL:
        raise ValueError(
            f"interface_names 一次最多 {MAX_INTERFACES_PER_PROPOSAL} 个接口，请分批操作"
        )
    if not all(isinstance(name, str) and validate_interface_name(name) for name in unique):
        raise ValueError(_INTERFACE_NAME_HINT)
    return unique


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
    # 命令接受的参数；没登记的参数一律拒绝。
    arguments: tuple[ArgName, ...] = ()
    # 仅 config-mode 命令（如端口开关）使用，写一个接口的那几行，按接口逐个展开；
    # 走 send_config_set 而非 send_command。
    config_templates: Mapping[VendorName, tuple[str, ...]] | None = None
    # 仅需要人工确认提示的 exec-mode 命令使用：按顺序登记每一轮提示与应答。
    confirmation: Mapping[VendorName, tuple[CommandConfirmation, ...]] | None = None
    # 发出后拿不到成功证据（重启会断开连接）：结果交给人工核实，不标成已执行。
    verify_manually: bool = False


class UnknownDeviceCommandError(ValueError):
    """请求的命令名不在目录里，在分配任何资源前就该拒绝。"""


class UnsupportedVendorError(ValueError):
    """命令存在，但目录里没有为这个厂商登记模板——跟"未知命令名"是两种不同原因。"""


_DEVICE_COMMAND_CATALOG: dict[CommandName, DeviceCommandDefinition] = {
    "show_version": DeviceCommandDefinition(
        name="show_version",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="查看设备版本信息",
        command_type="read_only",
        templates={
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
        description="从设备本机发起连通性测试：固定探测 1.1.1.1（非用户参数，避免被当探测跳板）",
        command_type="read_only",
        templates={
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
        description="重启设备（reload 语义）；执行前会等待设备确认提示",
        command_type="state_changing",
        templates={
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
    "port_enable": DeviceCommandDefinition(
        name="port_enable",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description=(
            "启用一组网络接口（no shutdown / undo shutdown 语义）；接口全名放在 interface_names 里"
        ),
        command_type="state_changing",
        templates={},
        arguments=("interface_names",),
        config_templates={
            "cisco_iosxe": ("interface {interface}", "no shutdown"),
            "cisco_small_business": ("interface {interface}", "no shutdown"),
            "huawei_vrp": ("interface {interface}", "undo shutdown"),
            # H3C Comware 与华为一样：进接口视图后 undo shutdown。Netmiko 会先发 system-view。
            "hp_comware": ("interface {interface}", "undo shutdown"),
            "juniper_junos": ("delete interfaces {interface} disable",),
        },
    ),
    "port_disable": DeviceCommandDefinition(
        name="port_disable",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="禁用一组网络接口（shutdown 语义）；接口全名放在 interface_names 里",
        command_type="state_changing",
        templates={},
        arguments=("interface_names",),
        config_templates={
            "cisco_iosxe": ("interface {interface}", "shutdown"),
            "cisco_small_business": ("interface {interface}", "shutdown"),
            "huawei_vrp": ("interface {interface}", "shutdown"),
            "hp_comware": ("interface {interface}", "shutdown"),
            "juniper_junos": ("set interfaces {interface} disable",),
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


def command_vendors(definition: DeviceCommandDefinition) -> tuple[str, ...]:
    """这条命令支持的厂商：exec 模板和 config 模板登记的都算。"""
    return tuple(sorted({*definition.templates, *(definition.config_templates or {})}))


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


def list_command_names_by_type(command_type: CommandType) -> tuple[str, ...]:
    """按风险分级返回命令名。

    给模型看的工具描述、策略报错文案都从这里取：目录加减命令时那些文案跟着变，
    不会出现「目录里有、描述里没有」——模型看不到的命令等于不存在。
    """
    return tuple(
        definition.name
        for definition in _DEVICE_COMMAND_CATALOG.values()
        if definition.command_type == command_type
    )


def list_vendor_names() -> tuple[str, ...]:
    """目录支持的全部厂商值（含占位厂商 other），给前端下拉和校验用。"""
    return get_args(VendorName.__value__)


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


def normalize_command_arguments(
    command_name: str, payload: Mapping[str, object]
) -> CommandArguments:
    """按目录登记的参数校验并归一化载荷里的参数字段。

    命令能接哪些参数只由目录说了算：登记过的必须给且合法，没登记的给了就拒绝。
    建提案、执行前复检、执行器三处都调它，保证证据快照里的命令行就是真正下发的那一行。
    不合法时抛 ValueError；原因里不带输入值，可以直接转给模型让它自己改。
    """
    definition = get_device_command(command_name)
    normalized: CommandArguments = {}
    for name in _ARG_NAMES:
        value = payload.get(name)
        if name not in definition.arguments:
            if value is not None:
                raise ValueError(f"命令 {command_name} 不接受 {name} 参数")
            continue
        if value is None:
            raise ValueError(f"命令 {command_name} 需要合法的接口名{_ARG_HINTS[name]} {name}")
        if name == "interface_names":
            if not isinstance(value, list):
                raise ValueError("interface_names 必须是接口全名列表")
            normalized["interface_names"] = list(normalize_interface_names(value))
        else:
            if not isinstance(value, str) or not validate_interface_name(value):
                raise ValueError(_INTERFACE_NAME_HINT)
            normalized["interface_name"] = value
    return normalized


def config_command_blocks(
    command_name: str, vendor: str, interface_names: Sequence[str]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """config 模式命令按接口展开：每个接口一组填好的命令行，不含整批最后的提交命令。

    执行器按这个分组逐口下发，出错在哪一组就是哪个口。
    """
    definition = get_device_command(command_name)
    if definition.config_templates is None or vendor not in definition.config_templates:
        raise UnsupportedVendorError(
            f"vendor {vendor!r} has no config template for command {command_name!r}"
        )
    templates = definition.config_templates[vendor]  # type: ignore[index]
    return tuple(
        (name, tuple(line.format(interface=name) for line in templates))
        for name in interface_names
    )


def rendered_command_lines(
    command_name: str, vendor: str, *, arguments: CommandArguments | None = None
) -> tuple[str, ...]:
    """返回这条命令在该厂商上实际下发的全部命令行。

    config 模式按接口逐个展开模板，最后追加厂商的提交命令；exec 模式一行，带占位符的
    模板在这里填好参数——绝不能把 {interface} 原样发到设备上。执行器和审批证据快照都
    从这里取，保证证据里记的就是真正发出去的内容。
    """
    args: CommandArguments = arguments or {}
    definition = get_device_command(command_name)
    if definition.config_templates is not None and vendor in definition.config_templates:
        blocks = config_command_blocks(command_name, vendor, args.get("interface_names", ()))
        lines = tuple(line for _, block in blocks for line in block)
        commit_lines: tuple[str, ...] = CONFIG_COMMIT_COMMANDS.get(vendor, ())  # type: ignore[call-overload]
        return lines + commit_lines

    template = get_command_template(command_name, vendor)
    if "{" not in template:
        return (template,)
    return (template.format(**_template_placeholders(args)),)


def _template_placeholders(arguments: CommandArguments) -> dict[str, str]:
    """把参数值映射成模板里的占位符；加新参数时只改这里和模板。"""
    placeholders: dict[str, str] = {}
    interface_name = arguments.get("interface_name")
    if interface_name is not None:
        placeholders["interface"] = interface_name
    return placeholders


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
