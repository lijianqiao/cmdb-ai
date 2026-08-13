/** 系统运行配置 API 响应与更新载荷类型 */

export type ConfigValueSource = "database" | "environment" | "unset"

export interface LlmSystemConfig {
  chat_base_url: string
  chat_model: string
  chat_input_cost_per_million_usd: number
  chat_output_cost_per_million_usd: number
  chat_api_key_configured: boolean
  chat_api_key_source: ConfigValueSource
  embedding_base_url: string
  embedding_model: string
  embedding_api_key_configured: boolean
  embedding_api_key_source: ConfigValueSource
}

export interface OperationsSystemConfig {
  hitl_notify_auto_approve: boolean
  monitor_probe_timeout_seconds: number
  monitor_sweep_interval_seconds: number
  cmdb_diff_interval_seconds: number
  monitor_event_retention_days: number
}

export interface SystemConfigData {
  llm: LlmSystemConfig
  operations: OperationsSystemConfig
}

export interface LlmSystemConfigUpdate {
  chat_base_url: string
  chat_api_key?: string
  clear_chat_api_key: boolean
  chat_model: string
  chat_input_cost_per_million_usd: number
  chat_output_cost_per_million_usd: number
  embedding_base_url: string
  embedding_api_key?: string
  clear_embedding_api_key: boolean
  embedding_model: string
}

export type OperationsSystemConfigUpdate = OperationsSystemConfig
