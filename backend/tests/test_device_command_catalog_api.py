"""设备命令目录只读接口：前端不再手工同步命令名和厂商列表。

命令扩到三十条左右之后，后端目录和前端的几份副本迟早对不上——加了命令忘了改前端，
页面上就选不到；删了厂商忘了改前端，下拉里还留着一个建了也不能用的值。
"""

import pytest
from httpx import AsyncClient

from app.agent.device_commands import (
    DEVICE_COMMAND_CATALOG_VERSION,
    list_device_commands,
)
from tests.conftest import Headers

pytestmark = pytest.mark.asyncio

_CATALOG_URL = "/api/v1/device-commands/catalog"


async def test_catalog_returns_every_command_with_arguments_and_vendors(
    client: AsyncClient, auth_headers: Headers
) -> None:
    response = await client.get(_CATALOG_URL, headers=auth_headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["catalog_version"] == DEVICE_COMMAND_CATALOG_VERSION
    by_name = {item["name"]: item for item in data["commands"]}
    assert set(by_name) == {item.name for item in list_device_commands()}

    port_disable = by_name["port_disable"]
    assert port_disable["command_type"] == "state_changing"
    assert port_disable["arguments"] == ["interface_names"]
    # config 模式登记的厂商也要算进「支持」，否则前端会以为端口命令没人支持。
    assert "hp_comware" in port_disable["vendors"]
    assert by_name["show_version"]["command_type"] == "read_only"
    assert by_name["show_version"]["arguments"] == []


async def test_catalog_returns_the_vendor_list_for_the_cmdb_form(
    client: AsyncClient, auth_headers: Headers
) -> None:
    response = await client.get(_CATALOG_URL, headers=auth_headers)

    vendors = response.json()["data"]["vendors"]
    assert "hp_comware" in vendors
    # 暂不支持的厂商占位值也要给，前端下拉要能选它登记设备。
    assert "other" in vendors
    # 已下线的主机厂商不能再出现。
    assert "linux" not in vendors
    assert "generic" not in vendors


async def test_catalog_reflects_a_newly_registered_command(
    client: AsyncClient, auth_headers: Headers, single_interface_read_only_command: str
) -> None:
    """目录里加一条命令，接口立刻就有：证明它是从目录生成的，不是另一份手工清单。"""
    response = await client.get(_CATALOG_URL, headers=auth_headers)

    by_name = {item["name"]: item for item in response.json()["data"]["commands"]}
    assert by_name[single_interface_read_only_command]["arguments"] == ["interface_name"]


async def test_catalog_requires_login(client: AsyncClient) -> None:
    """目录本身不敏感（都是代码常量），但也不对匿名开放。"""
    response = await client.get(_CATALOG_URL)

    assert response.status_code == 401
