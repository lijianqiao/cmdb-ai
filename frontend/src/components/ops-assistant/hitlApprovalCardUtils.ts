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
 * 批准按钮是否应禁用（含动态密码必填校验）
 */
export function isApproveButtonDisabled(
  deciding: boolean,
  detailLoading: boolean,
  needsPassword: boolean,
  password: string,
): boolean {
  if (deciding || detailLoading) return true
  if (needsPassword && !password.trim()) return true
  return false
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
