/** 监控目标相关类型 */

export type MonitorLatestStatus = "up" | "down"

/** 监控页轮询间隔（来自系统配置中的全局巡检间隔） */
export interface MonitorRuntime {
  sweep_interval_seconds: number
}

/** 监控目标（列表/详情响应，附带最近一次探测结果） */
export interface MonitorTarget {
  id: number
  cmdb_asset_id: number | null
  ip_address: string
  port: number
  label: string
  check_interval_seconds: number
  is_active: boolean
  created_at: string
  latest_status: MonitorLatestStatus | null
  latest_latency_ms: number | null
  latest_detail: string
  latest_checked_at: string | null
}

/** 创建监控目标请求 */
export interface MonitorTargetCreate {
  ip_address: string
  port: number
  label?: string
  check_interval_seconds?: number
  is_active?: boolean
  cmdb_asset_id?: number | null
}

/** 更新监控目标请求 */
export interface MonitorTargetUpdate {
  ip_address?: string
  port?: number
  label?: string
  check_interval_seconds?: number
  is_active?: boolean
  cmdb_asset_id?: number | null
}
