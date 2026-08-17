/** 系统运行配置 API 响应与更新载荷类型 */

export type ConfigValueSource = "database" | "environment" | "unset"

/** chat 三档：便宜（摘要/分类）、平衡（日常对话）、强（关键判断） */
export type ChatTier = "fast" | "balanced" | "strong"

export const CHAT_TIERS: readonly ChatTier[] = ["fast", "balanced", "strong"]

export const CHAT_TIER_LABELS: Record<ChatTier, string> = {
  fast: "便宜档",
  balanced: "平衡档",
  strong: "强档",
}

/** 单档 chat 的有效配置 */
export interface ChatTierConfig {
  base_url: string
  model: string
  input_cost_per_million_usd: number
  output_cost_per_million_usd: number
  api_key_configured: boolean
  api_key_source: ConfigValueSource
  /** false 表示这一档没配，下面的值全部来自 effective_tier 那一档 */
  configured: boolean
  effective_tier: ChatTier
}

export interface LlmSystemConfig {
  chat_fast: ChatTierConfig
  chat_balanced: ChatTierConfig
  chat_strong: ChatTierConfig
  embedding_base_url: string
  embedding_model: string
  embedding_api_key_configured: boolean
  embedding_api_key_source: ConfigValueSource
}

export interface OperationsSystemConfig {
  monitor_probe_timeout_seconds: number
  monitor_sweep_interval_seconds: number
  cmdb_diff_interval_seconds: number
  monitor_event_retention_days: number
}

export interface SystemConfigData {
  llm: LlmSystemConfig
  operations: OperationsSystemConfig
}

/** 单档 chat 的更新载荷；便宜档与强档留空 base_url + model 即"不配置" */
export interface ChatTierUpdate {
  base_url: string
  api_key?: string
  clear_api_key: boolean
  model: string
  input_cost_per_million_usd: number
  output_cost_per_million_usd: number
}

export interface LlmSystemConfigUpdate {
  chat_fast: ChatTierUpdate
  chat_balanced: ChatTierUpdate
  chat_strong: ChatTierUpdate
  embedding_base_url: string
  embedding_api_key?: string
  clear_embedding_api_key: boolean
  embedding_model: string
}

export type OperationsSystemConfigUpdate = OperationsSystemConfig
