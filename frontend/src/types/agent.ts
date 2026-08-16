/** Agent 会话 / 消息 / WebSocket 契约类型（对齐后端 schemas） */

/** 服务端推送的 WS 事件类型 */
export type AgentWsEventType =
  | "assistant_delta"
  | "tool_call"
  | "hitl_pending"
  | "hitl_resolved"
  | "hitl_execution_failed"
  | "child_status"
  | "monitor_alert"
  | "error"
  | "turn_done"

/** 服务端 → 客户端判别式消息 */
export interface AgentWsServerMessage {
  type: AgentWsEventType
  payload: Record<string, unknown>
}

/** 客户端可选首帧鉴权 */
export interface AgentWsClientAuth {
  type: "auth"
  access_token: string
}

/** 会话审批档位 */
export type ApprovalMode = "ask" | "assist" | "full"

/** 审批档位中文文案 */
export const APPROVAL_MODE_LABELS: Record<ApprovalMode, string> = {
  ask: "请求审批",
  assist: "帮我审批",
  full: "完全访问",
}

/** 会话列表/详情 */
export interface AgentSession {
  id: number
  user_id: number
  title: string
  status: string
  approval_mode: ApprovalMode
  created_at: string
  updated_at: string
}

/** 创建会话请求 */
export interface AgentSessionCreate {
  title?: string
}

/** 根 transcript 消息 */
export interface AgentMessage {
  id: number
  session_id: number
  role: string
  content: string
  tool_call_id: string | null
  tool_calls: Record<string, unknown>[] | null
  created_at: string
}

/** 发送用户消息 */
export interface AgentMessageCreate {
  content: string
}

/** 一轮对话结束后的 HTTP 摘要 */
export interface AgentChatTurn {
  reason: string
  final_answer: string | null
  control: string | null
}

/** 快照中暴露的 HITL 提案安全摘要（不含 action_payload 与凭据） */
export interface HitlProposalSafeSummary {
  proposal_id: number
  action_type: string
  status: string
  status_reason: string | null
  reason: string
  asset_id: number | null
  result_excerpt: string | null
  has_full_result: boolean
  created_at: string
  execution_started_at: string | null
  resolved_at: string | null
}

/** 已执行设备查询的按需完整结果（不进入聊天快照或 reducer） */
export interface DeviceQueryResult {
  proposal_id: number
  content: string
  content_length: number
  summary_status: "pending" | "generating" | "completed" | "fallback"
  created_at: string
}

/** 快照中暴露的子 Agent 安全摘要 */
export interface ChildAgentSnapshot {
  child_id: string
  role: string
  task_brief: string
  status: string
  result_summary: string | null
  created_at: string
  status_changed_at: string
}

/** 会话恢复快照：根消息分页 + 可恢复提案 + 子 Agent 摘要 */
export interface AgentSessionSnapshot {
  messages: AgentMessage[]
  proposals: HitlProposalSafeSummary[]
  children: ChildAgentSnapshot[]
  has_more_messages: boolean
  next_before_message_id: number | null
}
