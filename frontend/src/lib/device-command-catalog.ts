/** 设备命令目录（只读）REST 封装。
 *
 * 命令名、风险分级、参数和厂商列表都从后端目录接口取。以前前端各自抄了一份：
 * 后端加命令、删厂商时忘了同步，页面上就会少一个选项、或者留着一个建了也没用的值。
 */

import api from "@/lib/api"
import type { ApiResponse } from "@/types/api"

export interface DeviceCommandCatalogItem {
  name: string
  description: string
  command_type: "read_only" | "state_changing"
  /** 这条命令接受的参数名，如端口启停的 interface_names */
  arguments: string[]
  /** 支持这条命令的厂商 */
  vendors: string[]
}

export interface DeviceCommandCatalog {
  catalog_version: string
  commands: DeviceCommandCatalogItem[]
  vendors: string[]
  /** 登录后要执行 enable 提权的厂商：CMDB 表单只在这些厂商下显示 enable 口令 */
  enable_password_vendors: string[]
}

/** 目录还没加载回来时用它，页面不会因为 undefined 崩掉。 */
export const EMPTY_DEVICE_COMMAND_CATALOG: DeviceCommandCatalog = {
  catalog_version: "",
  commands: [],
  vendors: [],
  enable_password_vendors: [],
}

/** 拉取设备命令目录。 */
export async function fetchDeviceCommandCatalog(): Promise<DeviceCommandCatalog> {
  const response = await api.get<ApiResponse<DeviceCommandCatalog>>(
    "/device-commands/catalog"
  )
  return response.data.data
}

/** 会改变设备状态的命令名集合：表单据此把范围锁成「单台设备」并给出提示。 */
export function stateChangingCommandNames(
  catalog: DeviceCommandCatalog
): Set<string> {
  return new Set(
    catalog.commands
      .filter((item) => item.command_type === "state_changing")
      .map((item) => item.name)
  )
}
