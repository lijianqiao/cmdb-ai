"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: device_commands.py
@DateTime: 2026-09-23
@Docs: 设备命令目录只读接口：把代码里的命令目录和厂商列表给前端，避免手工抄第二份。

实现流程：
1. 只读、不碰数据库：内容是代码层常量（app/agent/device_commands.py），
   改动要走代码 review，不是运行时可配的。
2. 任何登录用户都能看：命令策略页要命令名和风险分级，CMDB 资产表单要厂商列表，
   两个页面的权限点不同，所以这里只要求登录，不绑某一个业务权限。
"""

from fastapi import APIRouter, Depends

from app.agent.device_commands import (
    DEVICE_COMMAND_CATALOG_VERSION,
    command_vendors,
    list_device_commands,
    list_vendor_names,
)
from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.common import ResponseEnvelope, success_response
from app.schemas.device_command_catalog import (
    DeviceCommandCatalogItem,
    DeviceCommandCatalogResponse,
)

router = APIRouter()


@router.get(
    "/catalog",
    response_model=ResponseEnvelope[DeviceCommandCatalogResponse],
    summary="设备命令目录",
)
async def get_device_command_catalog(
    _: User = Depends(get_current_user),
) -> ResponseEnvelope[DeviceCommandCatalogResponse]:
    """返回代码里登记的命令目录与厂商列表。"""
    commands = [
        DeviceCommandCatalogItem(
            name=definition.name,
            description=definition.description,
            command_type=definition.command_type,
            arguments=list(definition.arguments),
            vendors=list(command_vendors(definition)),
        )
        for definition in list_device_commands()
    ]
    return success_response(
        DeviceCommandCatalogResponse(
            catalog_version=DEVICE_COMMAND_CATALOG_VERSION,
            commands=commands,
            vendors=list(list_vendor_names()),
        )
    )
