/** Agent 会话 REST 封装（复用 `@/lib/api`，不新建 axios 实例） */

import api from "@/lib/api"
import type { ApiResponse, PaginatedData, PaginationParams } from "@/types/api"
import type {
  ApprovalMode,
  AgentChatTurn,
  AgentMessage,
  AgentMessageCreate,
  AgentSession,
  AgentSessionCreate,
  AgentSessionSnapshot,
  DeviceQueryResult,
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
 * 更新会话审批档位。
 *
 * Args:
 *   sessionId: 会话 ID
 *   body: 目标档位
 *
 * Returns:
 *   更新后的会话
 */
export async function patchAgentSession(
  sessionId: number,
  body: { approval_mode: ApprovalMode },
): Promise<AgentSession> {
  const response = await api.patch<ApiResponse<AgentSession>>(
    `/agent/sessions/${sessionId}`,
    body,
  )
  return response.data.data
}

/**
 * 硬删除会话（仅所有者；关联消息等由后端 CASCADE）。
 *
 * Args:
 *   sessionId: 会话 ID
 */
export async function deleteAgentSession(sessionId: number): Promise<void> {
  await api.delete(`/agent/sessions/${sessionId}`)
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
 * 获取会话恢复快照（消息分页 + 可恢复提案 + 子 Agent 摘要）。
 *
 * Args:
 *   sessionId: 会话 ID
 *   params: cursor 分页参数
 *   signal: 可选 AbortSignal，用于取消过期请求
 *
 * Returns:
 *   会话快照
 */
export async function getAgentSessionSnapshot(
  sessionId: number,
  params: { before_message_id?: number; limit?: number } = {},
  signal?: AbortSignal,
): Promise<AgentSessionSnapshot> {
  const response = await api.get<ApiResponse<AgentSessionSnapshot>>(
    `/agent/sessions/${sessionId}/snapshot`,
    { params, signal },
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

/** 点击展开后按会话归属读取已保存的完整设备查询结果。 */
export async function getDeviceQueryResult(
  sessionId: number,
  proposalId: number,
): Promise<DeviceQueryResult> {
  const response = await api.get<ApiResponse<DeviceQueryResult>>(
    `/agent/sessions/${sessionId}/device-query-results/${proposalId}`,
  )
  return response.data.data
}

/** 对已保存的设备查询正文恢复 AI 总结，不重新连接设备。 */
export async function recoverDeviceQuerySummary(
  sessionId: number,
  proposalId: number,
): Promise<DeviceQueryResult> {
  const response = await api.post<ApiResponse<DeviceQueryResult>>(
    `/agent/sessions/${sessionId}/device-query-results/${proposalId}/summary`,
  )
  return response.data.data
}
