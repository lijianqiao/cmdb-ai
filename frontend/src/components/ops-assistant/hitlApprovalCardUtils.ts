/** HitlApprovalCard / HitlApprovalDialog 纯函数与数据转换工具 */

import { isAxiosError } from "axios"
import type { HitlProposal } from "@/lib/hitl-api"

/**
 * 转换 HITL 审批状态显示文案
 */
export function statusLabel(status: string): string {
  switch (status.trim().toUpperCase()) {
    case "PENDING":
      return "等待审批"
    case "REJECTED":
      return "已拒绝"
    case "EXECUTED":
      return "已执行"
    case "APPROVED":
    case "EXECUTION_FAILED":
      return "已批准但未执行"
    case "UNKNOWN":
      return "执行结果不确定"
    default:
      return status || "未知状态"
  }
}

/**
 * 统一提取 Axios 或未知异常中的错误描述信息
 */
export function readErrorMessage(error: unknown, fallback: string): string {
  if (!isAxiosError(error)) return fallback
  const data = error.response?.data
  if (data && typeof data === "object") {
    const message = (data as { message?: unknown }).message
    if (typeof message === "string" && message.trim()) return message
    const detail = (data as { detail?: unknown }).detail
    if (typeof detail === "string" && detail.trim()) return detail
  }
  return fallback
}

/**
 * 从提案载荷中解析说明与目标资产 ID
 */
export function readPayloadMeta(proposal: HitlProposal): {
  reason: string
  assetId: number | null
} {
  const payload = proposal.action_payload
  const rawReason = payload?.proposal_reason
  const reason = typeof rawReason === "string" ? rawReason : ""
  const rawAsset = payload?.asset_id
  const assetId =
    typeof rawAsset === "number" && Number.isInteger(rawAsset) ? rawAsset : null
  return { reason, assetId }
}

/**
 * 是否应在卡片或弹窗上展示执行结果摘要
 */
export function shouldShowResultExcerpt(
  status: string,
  resultExcerpt: string | null | undefined,
): boolean {
  if (status.trim().toUpperCase() !== "EXECUTED") return false
  return typeof resultExcerpt === "string" && resultExcerpt.trim().length > 0
}

/**
 * 批准或重试设备命令时是否需输入动态凭据密码
 */
export function needsDynamicCredentialPassword(
  actionType: string,
  assetCredentialType: string | null | undefined,
): boolean {
  return (
    (actionType === "device_query" || actionType === "device_control") &&
    assetCredentialType === "dynamic"
  )
}

/**
 * 判断能否发出批准/重试请求所需的全部状态
 *
 * 按钮的禁用状态和点击处理函数用同一份判定：只禁用按钮不够，
 * 处理函数里要再查一次，防止程序化触发或旧闭包绕过按钮限制。
 */
export interface HitlSubmitState {
  canApprove: boolean
  deciding: boolean
  /** 组件眼下认定的最新状态 */
  status: string
  proposalId: number
  detail: HitlProposal | null | undefined
  detailLoading: boolean
  detailError: string | null | undefined
  needsPassword: boolean
  password: string
}

/**
 * 批准和重试共同的前置条件
 *
 * 审批人必须先看到这个提案的真实载荷（具体命令、接口参数）才能放行：
 * 详情还在加载、加载失败、或是切换提案后留下的旧详情，都不算「看过」。
 * 安全摘要只有动作类别、资产 ID 和原因，不能替代载荷。
 */
function passesSubmitPreconditions(state: HitlSubmitState): boolean {
  if (!state.canApprove || state.deciding) return false
  if (state.detailLoading || state.detailError) return false
  if (state.detail == null || state.detail.id !== state.proposalId) return false
  if (state.needsPassword && !state.password.trim()) return false
  return true
}

/**
 * 是否允许发出「批准」请求（仅 PENDING）
 *
 * 拒绝不走这里：拒绝不会在设备上执行任何东西，不需要先看载荷，也不需要设备密码。
 */
export function canSubmitApproval(state: HitlSubmitState): boolean {
  const normalized = state.status.trim().toUpperCase()
  const isPending = normalized === "PENDING" || normalized === ""
  return isPending && passesSubmitPreconditions(state)
}

/**
 * 是否允许发出「重试执行」请求：前置条件与批准相同，但仅 APPROVED
 */
export function canSubmitRetry(state: HitlSubmitState): boolean {
  return (
    isRetryAvailable(state.canApprove, state.status) &&
    passesSubmitPreconditions(state)
  )
}

/**
 * 从提案载荷中读取上次执行失败的分类信息
 */
export function readLastError(
  payload: Record<string, unknown> | null | undefined,
): string | null {
  const value = payload?.last_error
  return typeof value === "string" && value.trim() ? value : null
}

/**
 * 是否展示「重试执行」操作（仅 APPROVED 且有审批权限）
 */
export function isRetryAvailable(canApprove: boolean, status: string): boolean {
  return canApprove && status.trim().toUpperCase() === "APPROVED"
}

/**
 * 是否展示 UNKNOWN 人工处置操作（仅 UNKNOWN 且有审批权限）
 */
export function isUnknownResolutionAvailable(
  canApprove: boolean,
  status: string,
): boolean {
  return canApprove && status.trim().toUpperCase() === "UNKNOWN"
}
