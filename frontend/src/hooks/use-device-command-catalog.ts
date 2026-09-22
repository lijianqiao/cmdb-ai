/** 设备命令目录 hook。
 *
 * 目录是代码层常量，一个版本里不会变：模块级缓存一次，命令策略页和 CMDB 表单共用，
 * 不每次挂载都请求。失败不缓存，下次挂载还会再试——否则一次网络抖动会让下拉一直空着。
 */

import { useEffect, useState } from "react"

import {
  EMPTY_DEVICE_COMMAND_CATALOG,
  fetchDeviceCommandCatalog,
  type DeviceCommandCatalog,
} from "@/lib/device-command-catalog"

let cached: DeviceCommandCatalog | null = null
let inflight: Promise<DeviceCommandCatalog> | null = null

/** 清掉缓存，供测试使用。 */
export function resetDeviceCommandCatalogCache(): void {
  cached = null
  inflight = null
}

export function useDeviceCommandCatalog(): {
  catalog: DeviceCommandCatalog
  loading: boolean
} {
  const [catalog, setCatalog] = useState<DeviceCommandCatalog>(
    cached ?? EMPTY_DEVICE_COMMAND_CATALOG
  )
  const [loading, setLoading] = useState(cached === null)

  useEffect(() => {
    if (cached) return
    let active = true
    // 同一时刻只发一个请求：两个页面同时挂载时共用这一个 promise。
    const request = (inflight ??= fetchDeviceCommandCatalog())
    request
      .then((data) => {
        cached = data
        if (active) {
          setCatalog(data)
          setLoading(false)
        }
      })
      .catch(() => {
        inflight = null
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [])

  return { catalog, loading }
}
