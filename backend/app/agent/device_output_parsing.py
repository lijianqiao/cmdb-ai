"""把设备命令的原始输出按 TextFSM 模板解析成逐行的结构化数据（P4）。

实现流程：
1. 模板来自 ntc-templates（Netmiko 自带的依赖）：社区按「平台 + 命令」维护的一批
   正则模板，能把 display arp、display interface brief 这类表格输出拆成一行一行的
   字段（ip_address、interface、phy……）。华为、H3C 常用的查询命令大多有模板。
2. 解析只在要用的时候做：原文照样完整存库，这里拿原文和「实际下发的那一行命令」
   现场解析。不另存一份结构化数据，就不用改表结构；模板库升级后，旧结果也按新
   模板解析。
3. 模板对不上是常态：设备软件版本不同、命令没有模板、输出里夹了一行模板不认识
   的内容（有的模板遇到陌生行会直接报错）。这些情况一律返回 None，调用方照旧用
   原文——解析失败绝不能影响总结和执行结果。
4. 结构化结果只是「帮着逐条核对」的辅助，最终依据永远是设备原文。
"""

from collections.abc import Mapping
from typing import Any

from netmiko.utilities import get_structured_data_textfsm

# 我们的厂商值 → ntc-templates 里的平台名。和 Netmiko 连接用的 device_type 不是一回事：
# 比如华为连接用 huawei，模板库里叫 huawei_vrp；IOS-XE 的模板都登记在 cisco_ios 下。
_TEXTFSM_PLATFORMS: Mapping[str, str] = {
    "huawei_vrp": "huawei_vrp",
    "hp_comware": "hp_comware",
    "cisco_iosxe": "cisco_ios",
    "cisco_small_business": "cisco_s300",
    "juniper_junos": "juniper_junos",
}


def parse_device_output(
    vendor: str, command_line: str, output: str
) -> list[dict[str, Any]] | None:
    """按 TextFSM 模板把一条命令的原始输出解析成逐行字段。

    Args:
        vendor: 资产厂商（目录里的厂商值）。
        command_line: 实际下发给设备的那一行命令（带参数也能匹配模板，如
            display arp 10.1.1.1）。
        output: 设备返回的原始输出。

    Returns:
        解析出至少一行时返回行列表（每行是字段名到值的字典）；厂商没有模板库、
        命令没有模板、模板对不上或者什么都没解析出来时返回 None。
    """
    platform = _TEXTFSM_PLATFORMS.get(vendor)
    if platform is None or not output.strip():
        return None
    try:
        parsed = get_structured_data_textfsm(output, platform=platform, command=command_line)
    except Exception:
        # 模板遇到不认识的行会抛 TextFSMError；任何解析问题都退回原文，不往外抛。
        return None
    # 没有对应模板时 Netmiko 原样返回字符串。
    if not isinstance(parsed, list) or not parsed:
        return None
    return [dict(row) for row in parsed if isinstance(row, dict)] or None
