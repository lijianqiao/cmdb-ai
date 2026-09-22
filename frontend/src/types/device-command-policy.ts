/** 设备命令策略相关类型 */

export type PolicyScope = "asset_type" | "asset"
export type PolicyDecision = "whitelist" | "blacklist"

export interface DeviceCommandPolicy {
  id: number
  scope: PolicyScope
  asset_type: string | null
  asset_id: number | null
  command_name: string
  decision: PolicyDecision
  note: string
  created_by_user_id: number | null
  created_at: string
  updated_at: string
  asset?: {
    id: number
    hostname: string
    ip_address: string
    asset_type: string
  } | null
}

export interface DeviceCommandPolicyCreate {
  scope: PolicyScope
  asset_type?: string | null
  asset_id?: number | null
  command_name: string
  decision: PolicyDecision
  note?: string
}

export interface DeviceCommandPolicyUpdate {
  decision?: PolicyDecision
  note?: string
}

// 命令清单和风险分级不再在前端写一份：改从后端命令目录接口取，
// 见 lib/device-command-catalog.ts 与 hooks/use-device-command-catalog.ts。
