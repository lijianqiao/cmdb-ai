/** Agent 会话 REST 封装（复用 `@/lib/api`，不新建 axios 实例） */

import api from "@/lib/api"
import type { ApiResponse, PaginatedData, PaginationParams } from "@/types/api"
import type {
  AgentChatTurn,
  AgentMessage,
  AgentMessageCreate,
  AgentSession,
  AgentSessionCreate,
} from "@/types/agent"

/**
 * POST /agent/sessions/{id}/messages 单独超时。
 * 全局 axios 默认 30s，整轮 Agent turn（含 LLM）经常更长；
 * 此处放宽到 5 分钟，避免 WS 已推完而 HTTP 仍因超时失败。
 */
const POST_MESSAGE_TIMEOUT_MS = 300_000

/**
 * 创建 Agent 会话。
 *
 * Args:
 *   body: 可选标题
 *
 * Returns:
 *   新建会话
 */
export async function createAgentSession(
  body: AgentSessionCreate = {},
): Promise<AgentSession> {
  const response = await api.post<ApiResponse<AgentSession>>(
    "/agent/sessions",
    body,
  )
  return response.data.data
}

/**
 * 分页列出当前用户的会话。
 *
 * Args:
 *   params: page / page_size 等
 *
 * Returns:
 *   PaginatedData（items + total）
 */
export async function listAgentSessions(
  params: PaginationParams = {},
): Promise<PaginatedData<AgentSession>> {
  const response = await api.get<ApiResponse<PaginatedData<AgentSession>>>(
    "/agent/sessions",
    { params },
  )
  return response.data.data
}

/**
 * 获取会话详情。
 *
 * Args:
 *   sessionId: 会话 ID
 *
 * Returns:
 *   会话详情
 */
export async function getAgentSession(sessionId: number): Promise<AgentSession> {
  const response = await api.get<ApiResponse<AgentSession>>(
    `/agent/sessions/${sessionId}`,
  )
  return response.data.data
}

/**
 * 列出会话根 transcript 消息（非分页数组）。
 *
 * Args:
 *   sessionId: 会话 ID
 *
 * Returns:
 *   消息数组
 */
export async function listAgentMessages(
  sessionId: number,
): Promise<AgentMessage[]> {
  const response = await api.get<ApiResponse<AgentMessage[]>>(
    `/agent/sessions/${sessionId}/messages`,
  )
  return response.data.data
}

/**
 * 发送用户消息并触发一轮 Agent turn。
 *
 * Args:
 *   sessionId: 会话 ID
 *   body: 消息正文
 *
 * Returns:
 *   回合摘要（实时细节走 WebSocket）
 */
export async function postAgentMessage(
  sessionId: number,
  body: AgentMessageCreate,
): Promise<AgentChatTurn> {
  const response = await api.post<ApiResponse<AgentChatTurn>>(
    `/agent/sessions/${sessionId}/messages`,
    body,
    { timeout: POST_MESSAGE_TIMEOUT_MS },
  )
  return response.data.data
}
