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

/** 前端展示用命令目录条目（跟后端 app/agent/device_commands.py 手动保持一致） */
export const DEVICE_COMMAND_NAMES = [
  "show_version",
  "show_running_config",
  "show_interfaces",
  "ping",
  "reboot",
  "shutdown",
  "port_enable",
  "port_disable",
] as const

/** 跟后端 app/agent/device_commands.py::command_type 手动保持一致，用于表单风险提示 */
export const STATE_CHANGING_COMMAND_NAMES = new Set([
  "reboot",
  "shutdown",
  "port_enable",
  "port_disable",
])

export function isStateChangingCommand(commandName: string): boolean {
  return STATE_CHANGING_COMMAND_NAMES.has(commandName)
}
