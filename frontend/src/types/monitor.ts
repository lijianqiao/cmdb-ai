/** 监控目标相关类型 */

export type MonitorLatestStatus = "up" | "down"

/** 监控页轮询间隔（来自系统配置中的全局巡检间隔） */
export interface MonitorRuntime {
  sweep_interval_seconds: number
}

/** 状态条里一格的状态。`unknown` 表示那一分钟没有任何探测记录 */
export type MonitorBucketState = "up" | "down" | "unknown"

/**
 * 最近一小时的可用率状态条。
 *
 * 后端在列表响应里一并返回，前端一次请求就能画完整条图，不用逐行追加请求。
 * 每格对应的时间 = `started_at + index * bucket_seconds`。
 */
export interface MonitorUptimeWindow {
  started_at: string
  bucket_seconds: number
  buckets: MonitorBucketState[]
  /** 窗口内一次探测都没有时为 null——不能显示成 100%，那是撒谎 */
  uptime_rate: number | null
}

/** 监控目标（列表/详情响应，附带最近一次探测结果与可用率状态条） */
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
  uptime_window: MonitorUptimeWindow
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

/** 单条监控状态变化日志 */
export interface MonitorLogItem {
  id: number
  target_id: number
  label: string
  ip_address: string
  port: number
  status: MonitorLatestStatus
  latency_ms: number | null
  detail: string
  checked_at: string
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
