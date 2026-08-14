/** Agent WebSocket 纯函数：URL 构造、envelope 解析、重连退避 */

import type { AgentWsEventType, AgentWsServerMessage } from "@/types/agent"

/** 已知服务端事件类型集合 */
const AGENT_WS_EVENT_TYPES = new Set<AgentWsEventType>([
  "assistant_delta",
  "tool_call",
  "hitl_pending",
  "hitl_resolved",
  "hitl_execution_failed",
  "child_status",
  "monitor_alert",
  "error",
  "turn_done",
])

/** 构造 WS URL 时可注入的 location 子集（便于单测） */
export interface AgentWsLocation {
  protocol: string
  host: string
}

/**
 * 构造 Agent WebSocket URL（经 Vite `/api` 代理，需 `ws: true`）。
 *
 * Args:
 *   sessionId: 会话 ID
 *   accessToken: JWT access_token（会做 URI 编码）
 *   location: 协议与 host；默认取 `globalThis.location`
 *
 * Returns:
 *   形如 `ws(s)://host/api/v1/ws/agent/{id}?access_token=...` 的完整 URL
 */
export function buildAgentWsUrl(
  sessionId: number | string,
  accessToken: string,
  location: AgentWsLocation = globalThis.location,
): string {
  const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:"
  const token = encodeURIComponent(accessToken)
  return `${wsProtocol}//${location.host}/api/v1/ws/agent/${sessionId}?access_token=${token}`
}

/**
 * 解析服务端推送的判别式 JSON；非法内容返回 null。
 *
 * Args:
 *   raw: WebSocket onmessage 的文本帧
 *
 * Returns:
 *   合法 `AgentWsServerMessage`，或 null
 */
export function parseAgentWsMessage(raw: string): AgentWsServerMessage | null {
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return null
  }

  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    return null
  }

  const record = data as Record<string, unknown>
  const type = record.type
  if (typeof type !== "string" || !AGENT_WS_EVENT_TYPES.has(type as AgentWsEventType)) {
    return null
  }

  const payload = record.payload
  if (payload === undefined) {
    return { type: type as AgentWsEventType, payload: {} }
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return null
  }

  return {
    type: type as AgentWsEventType,
    payload: payload as Record<string, unknown>,
  }
}

/**
 * 计算下一次重连等待毫秒数（指数退避，封顶 30s）。
 *
 * Args:
 *   attempt: 已失败次数，从 0 起（0 → 1s，1 → 2s，…）
 *
 * Returns:
 *   延迟毫秒数，最大 30_000
 */
export function nextReconnectDelay(attempt: number): number {
  const safeAttempt = Math.max(0, attempt)
  const delay = 1_000 * 2 ** safeAttempt
  return Math.min(delay, 30_000)
}
