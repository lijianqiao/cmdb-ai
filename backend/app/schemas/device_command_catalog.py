"""设备命令目录的只读响应模型。

内容全部来自代码里的命令目录（app/agent/device_commands.py），不碰数据库：
前端的命令名、厂商列表原来是手工抄一份，命令扩到三十条之后迟早对不上。
"""

from app.schemas.common import ApiModel


class DeviceCommandCatalogItem(ApiModel):
    """目录里的一条命令。"""

    name: str
    description: str
    command_type: str
    # 这条命令接受的参数名（如端口启停的 interface_names）；没有参数就是空列表。
    arguments: list[str]
    # 支持这条命令的厂商：exec 模板和 config 模板登记的都算。
    vendors: list[str]


class DeviceCommandCatalogResponse(ApiModel):
    """命令目录 + 厂商列表；catalog_version 变了说明命令或模板有改动。"""

    catalog_version: str
    commands: list[DeviceCommandCatalogItem]
    vendors: list[str]
