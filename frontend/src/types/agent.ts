/** Agent 会话 / 消息 / WebSocket 契约类型（对齐后端 schemas） */

/** 服务端推送的 WS 事件类型 */
export type AgentWsEventType =
  | "assistant_delta"
  | "tool_call"
  | "hitl_pending"
  | "hitl_resolved"
  | "hitl_execution_failed"
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

/** 会话列表/详情 */
export interface AgentSession {
  id: number
  user_id: number
  title: string
  status: string
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
