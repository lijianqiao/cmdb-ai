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
6. 命令还登记两件和"怎么跑"有关的事：
   - read_timeout_class：整份配置这类慢命令归 long（秒数见 executors 与配置项），
     和 show_version 共用 60 秒的话大配置必超时，而读超时算"连上之后失败"，
     只能落 UNKNOWN 等人工核实。
   - probes_target：会真的向 ip_address 指定的目标发包（ping、traceroute）。目标不在
     CMDB 登记范围内时不论审批档位都要人批一次（D2），判断落在 hitl.gate_action，
     靠这个标记而不是在那里写死命令名。
   - 输出怎么处理：exact_ip_filter（按 IP 查表只能子串匹配的厂商，再按完整地址过滤）、
     max_output_lines（整张表截断，附一行说明让模型改用 *_lookup 命令），都由执行器执行。
"""

import ipaddress
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
    # P2b-2a 排查命令：设备健康
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
    # P2b-2b：整张表 / 日志类，以及 traceroute
    "show_mac_table",
    "show_arp",
    "show_routes",
    "show_logs",
    "show_alarms",
    "show_acl",
    "traceroute",
]
type CommandType = Literal["read_only", "state_changing"]
# 读超时档位：整份配置这类慢命令和 show_version 不能共用一个读超时，否则大配置必超时，
# 而读超时是「连上之后失败」，只能落 UNKNOWN 等人工核实。秒数由配置项决定，见 executors。
type ReadTimeoutClass = Literal["short", "long"]
# 命令可以登记的参数名。interface_name 给需要单个接口的只读命令用（4.2 的
# show_interface_detail 等），interface_names 只给端口启停用，两者不混用。
type ArgName = Literal[
    "interface_name", "interface_names", "ip_address", "mac_address", "vlan_id"
]


class CommandArguments(TypedDict, total=False):
    """一条命令的参数值：键只能是目录登记过的参数名，值已经校验并归一化。"""

    interface_name: str
    interface_names: list[str]
    ip_address: str
    # 12 位小写十六进制、不带分隔符；渲染时再按厂商写法加分隔符。
    mac_address: str
    vlan_id: int

# t15：H3C Comware 补上端口启停。配置在 system-view 里立即生效，和华为一样不自动保存。
# t16：只面向网络设备——下线 linux/generic 厂商和只对主机有意义的 shutdown，新增占位厂商 other。
# t17：端口启停一次接一组接口；Junos 进私有候选配置，commit 改为整批最后提交一次。
# t18：ping 的目标由调用方给（ip_address 参数），不再固定探 1.1.1.1；命令登记读超时档位。
# t19：34 条排查用只读命令（只登记华为 / H3C），新增 mac_address、vlan_id 参数。
# t20：整张表 / 日志类命令与 traceroute；整张表的输出截断到 FULL_TABLE_MAX_OUTPUT_LINES 行。
DEVICE_COMMAND_CATALOG_VERSION = "t20-v1"

# 整张表（MAC / ARP / 路由 / ACL）最多交回这么多行。核心设备上这几张表可能上万行，
# 全交给总结服务会按块多次调模型，又慢又贵；要找具体条目本来就该用 *_lookup 命令。
FULL_TABLE_MAX_OUTPUT_LINES = 300

# 一条提案最多带的接口数：一台接入交换机的口数，超过要求分两次。
MAX_INTERFACES_PER_PROPOSAL = 48

# 校验参数时按这个顺序检查，报错信息也按这个顺序给，输出稳定好测。
_ARG_NAMES: tuple[ArgName, ...] = (
    "interface_name",
    "interface_names",
    "ip_address",
    "mac_address",
    "vlan_id",
)
# 缺参数时说「需要合法的 X」，X 用人看得懂的说法：报错会直接转给模型让它自己改。
_ARG_MISSING_HINTS: Mapping[ArgName, str] = {
    "interface_name": "接口名",
    "interface_names": "接口名列表",
    "ip_address": "目标 IP 地址",
    "mac_address": "MAC 地址",
    "vlan_id": "VLAN 号",
}
_INTERFACE_NAME_HINT = "接口名只能包含字母、数字、/、.、-，且不超过 64 个字符"
_IP_ADDRESS_HINT = "目标 IP 地址必须是可探测的单播地址，不接受主机名、组播或广播地址"
_MAC_ADDRESS_HINT = (
    "MAC 地址写成 aabb.ccdd.eeff、aabb-ccdd-eeff 或 aa:bb:cc:dd:ee:ff 这几种格式之一"
)
_VLAN_ID_HINT = "VLAN 号必须是 1–4094 的整数"
_VLAN_ID_RANGE = range(1, 4095)

# 命令级正则、按厂商 CLI 语法书写；只用于 send_interactive 匹配确认提示，
# 不接受任何运行时输入，跟 templates 一样是代码层常量。
_INTERFACE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9/.\-]{1,64}$")
_IPV4_BROADCAST = ipaddress.IPv4Address("255.255.255.255")
# 三种常见写法：思科的 aabb.ccdd.eeff、华为 / H3C 的 aabb-ccdd-eeff、通用的 aa:bb:cc:dd:ee:ff
# （以及 aa-bb-cc-dd-ee-ff）。两位一组时分隔符必须前后一致，不接受混着写。
_HEX4 = r"[0-9A-Fa-f]{4}"
_HEX2 = r"[0-9A-Fa-f]{2}"
_MAC_ADDRESS_PATTERN = re.compile(
    rf"^(?:{_HEX4}\.{_HEX4}\.{_HEX4}|{_HEX4}-{_HEX4}-{_HEX4}|{_HEX2}([:-]){_HEX2}(?:\1{_HEX2}){{4}})$"
)
# 模板里 {mac} 按厂商 CLI 的写法填：四位一组，中间用这个分隔符。
# 给新厂商登记带 {mac} 的模板时要在这里补一行（test_every_vendor_with_a_mac_template_...）。
_MAC_GROUP_SEPARATORS: Mapping[VendorName, str] = {
    "huawei_vrp": "-",
    "hp_comware": "-",
}
# 「IP 地址还没完」的字符：查 10.1.1.1 时，10.1.1.10、110.1.1.1 都要算作别的地址。
_ADDRESS_CONTINUATION = r"[0-9A-Fa-f:.]"

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


def validate_ip_address(value: str) -> bool:
    """目标地址必须是能真正探测的单播 IP。

    只接受 ipaddress 能解析的字面量：主机名交给人先解析——设备上的 DNS 未必可用，
    而且「ping 的目标到底是哪个 IP」要在审批卡片上一眼可见。组播、广播、未指定地址
    发出去也没有意义，一并拒掉。
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.is_multicast or address.is_unspecified:
        return False
    return address != _IPV4_BROADCAST


def normalize_mac_address(value: str) -> str:
    """把几种常见 MAC 写法统一成 12 位小写十六进制（不带分隔符）。

    载荷里存这种与厂商无关的形式，渲染时再按厂商加分隔符：资产换了厂商之后重试也对。
    不合法时抛 ValueError；原因里不带输入值。
    """
    if not _MAC_ADDRESS_PATTERN.fullmatch(value):
        raise ValueError(_MAC_ADDRESS_HINT)
    return re.sub(r"[.:-]", "", value).lower()


def filter_exact_ip_lines(output: str, ip_address: str) -> str:
    """只留下「被查的 IP 完整出现」或「根本没出现这个 IP」的行。

    华为按 IP 查 ARP 只能用 display arp | include，是子串匹配：查 10.1.1.1 会带出
    10.1.1.10～19、110.1.1.1。回答「这个 IP 接在哪个口」时混进别的地址就是错答。
    没出现 IP 的行（表头、命令回显以外的说明）原样保留，方便模型读懂列含义。
    """
    escaped = re.escape(ip_address)
    exact = re.compile(rf"(?<!{_ADDRESS_CONTINUATION}){escaped}(?!{_ADDRESS_CONTINUATION})")
    return "\n".join(
        line
        for line in output.splitlines()
        if ip_address not in line or exact.search(line)
    )


def truncate_output_lines(output: str, max_lines: int) -> tuple[str, bool]:
    """只保留前 max_lines 行，超出时在末尾注明总行数和精确查询的办法。

    保留开头：这几张表的表头在最前面，截掉后面的条目，模型仍然读得懂列含义。
    返回 (截断后的输出, 是否截断)。
    """
    lines = output.splitlines()
    if len(lines) <= max_lines:
        return output, False
    note = (
        f"……（输出共 {len(lines)} 行，只保留前 {max_lines} 行。要找具体条目请用 "
        "show_mac_lookup / show_arp_lookup / show_route_lookup 按条件精确查询）"
    )
    return "\n".join([*lines[:max_lines], note]), True


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
    # 读超时档位：输出可能很大、或者命令本身要跑很久的登记 long。
    read_timeout_class: ReadTimeoutClass = "short"
    # 会向 ip_address 指定的目标发包（ping/traceroute）。只查设备本地表的命令不算，
    # 哪怕它也带 ip_address。D2 据此决定「目标不在 CMDB 就一律转人工审批」。
    probes_target: bool = False
    # 某些厂商只能用「| include {ip}」按 IP 查（子串匹配）：执行器再按完整地址过滤一次，
    # 见 filter_exact_ip_lines。
    exact_ip_filter: bool = False
    # 输出行数上限：超出的部分由执行器截掉并附一行说明，见 truncate_output_lines。
    max_output_lines: int | None = None


class UnknownDeviceCommandError(ValueError):
    """请求的命令名不在目录里，在分配任何资源前就该拒绝。"""


class UnsupportedVendorError(ValueError):
    """命令存在，但目录里没有为这个厂商登记模板——跟"未知命令名"是两种不同原因。"""


def _read_only(
    name: CommandName,
    description: str,
    templates: Mapping[VendorName, str],
    *,
    arguments: tuple[ArgName, ...] = (),
    exact_ip_filter: bool = False,
    read_timeout_class: ReadTimeoutClass = "short",
    max_output_lines: int | None = None,
    probes_target: bool = False,
) -> DeviceCommandDefinition:
    """排查类只读命令的简写：风险分级、版本号都一样，只有名字、说明、模板和参数不同。"""
    return DeviceCommandDefinition(
        name=name,
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description=description,
        command_type="read_only",
        templates=templates,
        arguments=arguments,
        exact_ip_filter=exact_ip_filter,
        read_timeout_class=read_timeout_class,
        max_output_lines=max_output_lines,
        probes_target=probes_target,
    )


# P2b-2a：输出有限、按对象定位的排查命令。只登记华为 / H3C（现网主力设备）；
# 其它厂商没在真机上核对过，暂不登记 = 不支持。说明文字会原样给模型和前端看，
# 所以写「查什么、什么时候用」，而不是复述命令本身。
_TROUBLESHOOTING_COMMANDS: tuple[DeviceCommandDefinition, ...] = (
    # ---- 设备健康 ----
    _read_only(
        "show_cpu",
        "查看 CPU 使用率（设备卡顿、管理响应慢、怀疑环路时先看）",
        {"huawei_vrp": "display cpu-usage", "hp_comware": "display cpu-usage"},
    ),
    _read_only(
        "show_memory",
        "查看内存使用率",
        {"huawei_vrp": "display memory-usage", "hp_comware": "display memory"},
    ),
    _read_only(
        "show_environment",
        "查看板卡、风扇、电源等硬件状态（告警灯亮、设备异常重启时看）",
        {"huawei_vrp": "display device", "hp_comware": "display device"},
    ),
    _read_only(
        "show_stack",
        "查看堆叠 / IRF 状态：成员、主备、有没有分裂",
        {"huawei_vrp": "display stack", "hp_comware": "display irf"},
    ),
    _read_only(
        "show_clock",
        "查看设备当前时间（对日志时间、排查证书问题时用）",
        {"huawei_vrp": "display clock", "hp_comware": "display clock"},
    ),
    _read_only(
        "show_ntp",
        "查看 NTP 时间同步状态",
        {"huawei_vrp": "display ntp-service status", "hp_comware": "display ntp-service status"},
    ),
    # ---- 接口与链路 ----
    _read_only(
        "show_interfaces_description",
        "查看所有接口的描述（接口接的是什么设备、给哪个业务用）",
        {
            "huawei_vrp": "display interface description",
            "hp_comware": "display interface brief description",
        },
    ),
    _read_only(
        "show_interface_detail",
        "查看单个接口的详情：up/down、速率双工、错包计数、收发速率；接口全名放在 interface_name",
        {"huawei_vrp": "display interface {interface}", "hp_comware": "display interface {interface}"},
        arguments=("interface_name",),
    ),
    _read_only(
        "show_interface_config",
        "查看单个接口的配置（VLAN、描述、是否 shutdown 等）；比取整份配置快得多",
        {
            "huawei_vrp": "display current-configuration interface {interface}",
            "hp_comware": "display current-configuration interface {interface}",
        },
        arguments=("interface_name",),
    ),
    _read_only(
        "show_down_interfaces",
        "列出处于 down 状态的接口（H3C 会带出 down 的原因）",
        {
            "huawei_vrp": "display interface brief | include down",
            "hp_comware": "display interface brief down",
        },
    ),
    _read_only(
        "show_error_down",
        "查看被环路检测、端口安全等保护机制自动关掉的接口（error-down）",
        {"huawei_vrp": "display error-down recovery"},
    ),
    _read_only(
        "show_interface_errors",
        "查看各接口的错包统计，找出哪个口有 CRC 等错包（华为简表的 inErrors/outErrors 列）",
        {"huawei_vrp": "display interface brief", "hp_comware": "display counters inbound interface"},
    ),
    _read_only(
        "show_interface_traffic",
        "查看各接口的流量 / 利用率，找出哪个口被打满（H3C 只有入方向）",
        {
            "huawei_vrp": "display interface brief",
            "hp_comware": "display counters rate inbound interface",
        },
    ),
    _read_only(
        "show_transceiver",
        "查看单个光口的光模块信息与收发光功率（光口不通、时通时断时看）",
        {
            "huawei_vrp": "display transceiver interface {interface} verbose",
            "hp_comware": "display transceiver diagnosis interface {interface}",
        },
        arguments=("interface_name",),
    ),
    _read_only(
        "show_port_channel",
        "查看链路聚合（Eth-Trunk / 聚合组）及成员口状态",
        {"huawei_vrp": "display eth-trunk", "hp_comware": "display link-aggregation summary"},
    ),
    _read_only(
        "show_poe",
        "查看 PoE 供电状态（AP、IP 电话不亮时看）",
        {"huawei_vrp": "display poe power-state", "hp_comware": "display poe interface"},
    ),
    # ---- 二层：VLAN、MAC、生成树、邻居 ----
    _read_only(
        "show_vlan",
        "查看 VLAN 列表",
        {"huawei_vrp": "display vlan", "hp_comware": "display vlan brief"},
    ),
    _read_only(
        "show_vlan_detail",
        "查看单个 VLAN 包含哪些接口；VLAN 号放在 vlan_id",
        {"huawei_vrp": "display vlan {vlan}", "hp_comware": "display vlan {vlan}"},
        arguments=("vlan_id",),
    ),
    _read_only(
        "show_mac_lookup",
        "按 MAC 地址查它接在哪个接口、哪个 VLAN；MAC 放在 mac_address（常见写法都行）",
        {"huawei_vrp": "display mac-address {mac}", "hp_comware": "display mac-address {mac}"},
        arguments=("mac_address",),
    ),
    _read_only(
        "show_mac_on_interface",
        "查看某个接口上学到了哪些 MAC（这个口下面接了哪些终端）；接口全名放在 interface_name",
        {
            "huawei_vrp": "display mac-address {interface}",
            "hp_comware": "display mac-address interface {interface}",
        },
        arguments=("interface_name",),
    ),
    _read_only(
        "show_mac_in_vlan",
        "查看某个 VLAN 里学到了哪些 MAC；VLAN 号放在 vlan_id",
        {
            "huawei_vrp": "display mac-address vlan {vlan}",
            "hp_comware": "display mac-address vlan {vlan}",
        },
        arguments=("vlan_id",),
    ),
    _read_only(
        "show_mac_flapping",
        "查看 MAC 漂移记录：同一个 MAC 在多个口之间来回跳，是环路的典型症状",
        {"huawei_vrp": "display mac-address flapping", "hp_comware": "display mac-address mac-move"},
    ),
    _read_only(
        "show_stp",
        "查看生成树概况：各接口的角色和转发状态",
        {"huawei_vrp": "display stp brief", "hp_comware": "display stp brief"},
    ),
    _read_only(
        "show_stp_root",
        "查看生成树根桥是谁（根桥跑偏会导致流量绕路）",
        {"huawei_vrp": "display stp | include Root", "hp_comware": "display stp root"},
    ),
    _read_only(
        "show_stp_tc",
        "查看生成树拓扑变化统计：拓扑频繁变化会导致网络时断时续",
        {"huawei_vrp": "display stp tc-bpdu statistics", "hp_comware": "display stp tc"},
    ),
    _read_only(
        "show_lldp_neighbors",
        "查看 LLDP 邻居：每个接口对端连的是哪台设备、哪个口",
        {
            "huawei_vrp": "display lldp neighbor brief",
            "hp_comware": "display lldp neighbor-information list",
        },
    ),
    # ---- 三层与连通性 ----
    _read_only(
        "show_ip_interfaces",
        "查看三层接口（VLANIF / Vlan-interface 等）的 IP 与 up/down 简表",
        {"huawei_vrp": "display ip interface brief", "hp_comware": "display ip interface brief"},
    ),
    _read_only(
        "show_arp_lookup",
        "按 IP 查 ARP：这个 IP 对应哪个 MAC、从哪个接口学到（定位终端第一步）；IP 放在 ip_address",
        {"huawei_vrp": "display arp | include {ip}", "hp_comware": "display arp {ip}"},
        arguments=("ip_address",),
        # 华为只能子串匹配，查 10.1.1.1 会带出 10.1.1.10：执行器按完整地址再过滤一次。
        exact_ip_filter=True,
    ),
    _read_only(
        "show_route_lookup",
        "查到某个 IP 走哪条路由（下一跳、出接口）；IP 放在 ip_address",
        {
            "huawei_vrp": "display ip routing-table {ip}",
            "hp_comware": "display ip routing-table {ip}",
        },
        arguments=("ip_address",),
    ),
    _read_only(
        "show_default_route",
        "查看默认路由（出口不通时先看它在不在、下一跳对不对）",
        {
            "huawei_vrp": "display ip routing-table 0.0.0.0",
            "hp_comware": "display ip routing-table 0.0.0.0",
        },
    ),
    _read_only(
        "show_vrrp",
        "查看 VRRP 网关冗余状态：谁是 Master、有没有双主",
        {"huawei_vrp": "display vrrp brief", "hp_comware": "display vrrp"},
    ),
    _read_only(
        "show_ospf_neighbors",
        "查看 OSPF 邻居状态（没到 Full 说明邻居有问题）",
        {"huawei_vrp": "display ospf peer brief", "hp_comware": "display ospf peer"},
    ),
    _read_only(
        "show_bgp_summary",
        "查看 BGP 邻居概况（状态、收到的路由数）",
        {"huawei_vrp": "display bgp peer", "hp_comware": "display bgp peer ipv4"},
    ),
    _read_only(
        "show_dhcp_snooping_bindings",
        "查看 DHCP Snooping 绑定表：终端 IP、MAC、接口、VLAN 的对应关系（终端拿不到地址时看）",
        {
            "huawei_vrp": "display dhcp snooping user-bind all",
            "hp_comware": "display dhcp snooping binding",
        },
    ),
)

# P2b-2b：整张表 / 日志类，以及 traceroute。同样只登记华为 / H3C。
# 整张表截断到 FULL_TABLE_MAX_OUTPUT_LINES 行；日志和告警不截——命令本身已经限了条数，
# 而且华为日志的先后顺序没核对过，按行截断可能正好截掉最新的那几条。
_BULK_COMMANDS: tuple[DeviceCommandDefinition, ...] = (
    _read_only(
        "show_mac_table",
        "查看整张 MAC 地址表（输出很长，只保留前 300 行）；找某个 MAC 请用 show_mac_lookup，"
        "看某个口请用 show_mac_on_interface",
        {"huawei_vrp": "display mac-address", "hp_comware": "display mac-address"},
        read_timeout_class="long",
        max_output_lines=FULL_TABLE_MAX_OUTPUT_LINES,
    ),
    _read_only(
        "show_arp",
        "查看整张 ARP 表（输出很长，只保留前 300 行）；找某个 IP 请用 show_arp_lookup",
        {"huawei_vrp": "display arp", "hp_comware": "display arp"},
        read_timeout_class="long",
        max_output_lines=FULL_TABLE_MAX_OUTPUT_LINES,
    ),
    _read_only(
        "show_routes",
        "查看整张路由表（输出很长，只保留前 300 行）；查到某个 IP 走哪条路由请用 show_route_lookup",
        {"huawei_vrp": "display ip routing-table", "hp_comware": "display ip routing-table"},
        read_timeout_class="long",
        max_output_lines=FULL_TABLE_MAX_OUTPUT_LINES,
    ),
    _read_only(
        "show_logs",
        "查看设备最近 200 条日志（端口 up/down、环路告警、登录记录等，排查故障发生的时间点）",
        {
            "huawei_vrp": "display logbuffer size 200",
            # reverse：最新的排在最前面。
            "hp_comware": "display logbuffer reverse size 200",
        },
        read_timeout_class="long",
    ),
    _read_only(
        "show_alarms",
        "查看设备当前告警（华为）/ 最近 50 条告警（H3C）",
        {"huawei_vrp": "display alarm active", "hp_comware": "display trapbuffer reverse size 50"},
    ),
    _read_only(
        "show_acl",
        "查看 ACL 规则和命中计数（输出可能很长，只保留前 300 行）",
        {"huawei_vrp": "display acl all", "hp_comware": "display acl all"},
        max_output_lines=FULL_TABLE_MAX_OUTPUT_LINES,
    ),
    _read_only(
        "traceroute",
        "从设备本机做路由跟踪（最多 15 跳、每跳 1 秒超时），目标放在 ip_address（只接受 IP）；"
        "目标不在 CMDB 登记范围内时一律转人工审批",
        {
            "huawei_vrp": "tracert -m 15 -w 1000 {ip}",
            "hp_comware": "tracert -m 15 -w 1000 {ip}",
        },
        arguments=("ip_address",),
        read_timeout_class="long",
        # 和 ping 一样会真的向目标发包（D2）。
        probes_target=True,
    ),
)


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
        description=(
            "查看整份当前生效配置（输出长、可能包含敏感信息，建议默认不进白名单）；"
            "只看某个接口时用 show_interface_config，只在需要全局审阅配置时再取整份"
        ),
        command_type="read_only",
        # 大配置几千行，60 秒读不完；读超时属于「连上之后失败」，会落 UNKNOWN 等人工核实。
        read_timeout_class="long",
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
            "从设备本机发起连通性测试，目标放在 ip_address 里（只接受 IP，不接受主机名）；"
            "目标不在 CMDB 登记范围内时一律转人工审批"
        ),
        command_type="read_only",
        arguments=("ip_address",),
        # 会真的向目标发包：目标不在 CMDB 内时不论档位都要人批一次（D2）。
        probes_target=True,
        templates={
            # 各厂商都显式限制次数：默认次数各不相同，Junos 默认根本不停。
            "cisco_iosxe": "ping {ip} repeat 4",
            "cisco_small_business": "ping ip {ip}",
            "huawei_vrp": "ping -c 4 {ip}",
            "hp_comware": "ping -c 4 {ip}",
            "juniper_junos": "ping {ip} count 4",
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
    **{definition.name: definition for definition in _TROUBLESHOOTING_COMMANDS},
    **{definition.name: definition for definition in _BULK_COMMANDS},
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


def command_probes_target(command_name: str) -> bool:
    """这条命令会不会真的向 ip_address 指定的目标发包（D2 据此决定是否强制人工审批）。

    命令名未知时返回 False：未知命令会在别处被拒，这里不该顺便改变审批结论。
    """
    definition = _DEVICE_COMMAND_CATALOG.get(command_name)  # type: ignore[call-overload]
    return bool(definition and definition.probes_target)


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
            raise ValueError(f"命令 {command_name} 需要合法的{_ARG_MISSING_HINTS[name]} {name}")
        if name == "interface_names":
            if not isinstance(value, list):
                raise ValueError("interface_names 必须是接口全名列表")
            normalized["interface_names"] = list(normalize_interface_names(value))
        elif name == "ip_address":
            if not isinstance(value, str) or not validate_ip_address(value):
                raise ValueError(_IP_ADDRESS_HINT)
            normalized["ip_address"] = value
        elif name == "mac_address":
            if not isinstance(value, str):
                raise ValueError(_MAC_ADDRESS_HINT)
            normalized["mac_address"] = normalize_mac_address(value)
        elif name == "vlan_id":
            # bool 是 int 的子类：不单独拦的话 True 会被渲染成 display vlan True。
            if isinstance(value, bool) or not isinstance(value, int) or value not in _VLAN_ID_RANGE:
                raise ValueError(_VLAN_ID_HINT)
            normalized["vlan_id"] = value
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
    return (template.format(**_template_placeholders(args, vendor)),)


def _template_placeholders(arguments: CommandArguments, vendor: str) -> dict[str, str]:
    """把参数值映射成模板里的占位符；加新参数时只改这里和模板。

    MAC 要按厂商写法格式化，所以这里需要 vendor。
    """
    placeholders: dict[str, str] = {}
    interface_name = arguments.get("interface_name")
    if interface_name is not None:
        placeholders["interface"] = interface_name
    ip_address = arguments.get("ip_address")
    if ip_address is not None:
        placeholders["ip"] = ip_address
    mac_address = arguments.get("mac_address")
    if mac_address is not None:
        separator = _MAC_GROUP_SEPARATORS[vendor]  # type: ignore[index]
        placeholders["mac"] = separator.join(
            mac_address[index : index + 4] for index in range(0, 12, 4)
        )
    vlan_id = arguments.get("vlan_id")
    if vlan_id is not None:
        placeholders["vlan"] = str(vlan_id)
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
