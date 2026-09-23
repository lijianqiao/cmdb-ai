"""设备输出的结构化解析（P4）：模板对得上就逐行给字段，对不上一律返回 None、绝不抛错。"""

from app.agent.device_output_parsing import parse_device_output

_H3C_ARP_LOOKUP = """  Type: S-Static   D-Dynamic   O-Openflow   R-Rule   M-Multiport  I-Invalid
IP address      MAC address    VLAN/VSI name Interface                Aging Type
10.1.1.1        aabb-ccdd-0001 10            GE1/0/1                  1156  D
"""

_HUAWEI_INTERFACE_BRIEF = """PHY: Physical
*down: administratively down
(l): loopback
(s): spoofing
(b): BFD down
InUti/OutUti: input utility/output utility
Interface                   PHY   Protocol  InUti OutUti   inErrors  outErrors
GigabitEthernet0/0/1        up    up           0%     0%          0          0
GigabitEthernet0/0/2        down  down         0%     0%         12          0
"""


def test_h3c_arp_lookup_is_parsed_into_fields() -> None:
    """带参数的命令（display arp 10.1.1.1）也能对上模板：要的就是「这个 IP 在哪个口」。"""
    rows = parse_device_output("hp_comware", "display arp 10.1.1.1", _H3C_ARP_LOOKUP)

    assert rows == [
        {
            "ip_address": "10.1.1.1",
            "mac_address": "aabb-ccdd-0001",
            "vlan_id": "10",
            "interface": "GE1/0/1",
            "aging": "1156",
            "type": "D",
        }
    ]


def test_huawei_interface_brief_gives_state_and_errors_per_port() -> None:
    rows = parse_device_output("huawei_vrp", "display interface brief", _HUAWEI_INTERFACE_BRIEF)

    assert rows is not None
    by_port = {row["interface"]: row for row in rows}
    assert by_port["GigabitEthernet0/0/1"]["phy"] == "up"
    assert by_port["GigabitEthernet0/0/2"]["phy"] == "down"
    assert by_port["GigabitEthernet0/0/2"]["inerrors"] == "12"


def test_output_the_template_does_not_recognise_returns_none() -> None:
    """华为接口简表的模板遇到不认识的行会直接报错：这里必须兜住，交给调用方用原文。"""
    output = "some banner line the template has never seen\n" + _HUAWEI_INTERFACE_BRIEF

    assert parse_device_output("huawei_vrp", "display interface brief", output) is None


def test_command_without_a_template_returns_none() -> None:
    assert parse_device_output("huawei_vrp", "display ntp-service status", "clock status: synchronized") is None


def test_vendor_without_templates_returns_none() -> None:
    assert parse_device_output("other", "display arp", _H3C_ARP_LOOKUP) is None


def test_empty_output_returns_none() -> None:
    assert parse_device_output("hp_comware", "display arp", "   \n") is None
