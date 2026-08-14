"""CMDB CLI 平台到 Netmiko 官方驱动的映射测试；不建立真实网络连接。"""

from unittest.mock import MagicMock, patch

from app.agent.executors import (
    _netmiko_device_type_for_vendor,
    _open_netmiko_connection,
)


def test_cisco_platforms_use_distinct_netmiko_drivers() -> None:
    assert _netmiko_device_type_for_vendor("cisco_iosxe") == "cisco_xe"
    assert _netmiko_device_type_for_vendor("cisco_small_business") == "cisco_s300"


def test_open_small_business_connection_uses_cisco_s300_driver() -> None:
    connection = MagicMock()
    with patch("app.agent.executors.ConnectHandler", return_value=connection) as connect:
        result = _open_netmiko_connection(
            host="10.0.0.67",
            vendor="cisco_small_business",
            username="admin",
            password="test-only",
            conn_timeout=11.0,
        )

    assert result is connection
    connect.assert_called_once_with(
        device_type="cisco_s300",
        host="10.0.0.67",
        username="admin",
        password="test-only",
        conn_timeout=11.0,
        auth_timeout=11.0,
        banner_timeout=11.0,
    )
