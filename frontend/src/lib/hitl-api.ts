/** HITL 审批 REST 封装（复用 `@/lib/api`，不新建 axios 实例） */

import api from "@/lib/api"
import type { ApiResponse } from "@/types/api"

/** 审批人可见的 HITL 提案（含完整 action_payload） */
export interface HitlProposal {
  id: number
  session_id: number
  proposed_by_agent_id: string | null
  action_type: string
  action_payload: Record<string, unknown>
  status: string
  reviewed_by_user_id: number | null
  reviewed_at: string | null
  executed_at: string | null
  created_at: string
  result_excerpt?: string | null
  asset_credential_type?: string | null
}

export interface HitlDecideRequest {
  approve: boolean
  dynamic_credential_password?: string
}

export interface ListHitlProposalsParams {
  session_id: number
  status?: string
}

/**
 * 按会话列出 HITL 提案。
 *
 * Args:
 *   params: session_id 与可选 status 过滤
 *
 * Returns:
 *   提案数组（需 agent:hitl_approve）
 */
export async function listHitlProposals(
  params: ListHitlProposalsParams,
): Promise<HitlProposal[]> {
  const response = await api.get<ApiResponse<HitlProposal[]>>("/hitl/proposals", {
    params,
  })
  return response.data.data
}

/**
 * 获取单个 HITL 提案详情（含完整载荷）。
 *
 * Args:
 *   proposalId: 提案 ID
 *
 * Returns:
 *   提案详情
 */
export async function getHitlProposal(proposalId: number): Promise<HitlProposal> {
  const response = await api.get<ApiResponse<HitlProposal>>(
    `/hitl/proposals/${proposalId}`,
  )
  return response.data.data
}

/**
 * 批准或拒绝 HITL 提案。
 *
 * Args:
 *   proposalId: 提案 ID
 *   body: `{ approve: bool }`
 *
 * Returns:
 *   审批后的提案（批准后可能仍为 APPROVED，表示执行未完成）
 */
export async function decideHitlProposal(
  proposalId: number,
  body: HitlDecideRequest,
): Promise<HitlProposal> {
  const response = await api.post<ApiResponse<HitlProposal>>(
    `/hitl/proposals/${proposalId}/decide`,
    body,
  )
  return response.data.data
}
