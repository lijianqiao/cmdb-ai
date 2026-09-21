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
    case "EXECUTING":
      return "执行中"
    case "UNKNOWN":
      return "执行结果不确定"
    default:
      return status || "未知状态"
  }
}

/** 请求没拿到明确答复、正在按提案 ID 查询真实状态时的提示 */
export const HITL_RECONCILING_MESSAGE = "请求结果尚未确认，正在核对…"

/**
 * 决定卡片显示哪个状态：WS/快照推来的 prop，还是本卡片请求拿到的结果
 *
 * 两路到达的先后没有保证：迟到的 HTTP 响应可能比已经收到的 WS 事件更旧。
 * EXECUTED、REJECTED 是终态，出现后不会再变，所以 prop 已是终态时以它为准；
 * 否则以本卡片最近一次请求的结果为准（prop 一变，这个结果就会被清掉）。
 */
export function resolveDisplayStatus(
  propStatus: string,
  localStatus: string | null,
): string {
  const normalized = propStatus.trim().toUpperCase()
  if (normalized === "EXECUTED" || normalized === "REJECTED") return propStatus
  return localStatus ?? propStatus
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
 * 请求是否没拿到明确答复：超时、断网，或网关/服务端 5xx
 *
 * 批准和重试接口会同步等设备跑完才返回，所以这些情况下服务端很可能
 * 已经批准、甚至已经执行完了——不能报「失败」，只能按提案 ID 去查真实状态。
 * 4xx 是服务端明确拒绝了这次请求，照常报错即可。
 */
export function isOutcomeUnknownError(error: unknown): boolean {
  if (!isAxiosError(error)) return false
  const status = error.response?.status
  return status == null || status >= 500
}

/** 一条结果提示：用哪种 toast、说什么 */
export interface HitlOutcomeNotice {
  level: "success" | "info" | "warning"
  message: string
}

/**
 * 按提案的真实状态给出结果提示
 *
 * HTTP 200 只说明请求被处理了，不代表设备执行成功：只有 EXECUTED 才报成功。
 * 执行中、结果不确定（UNKNOWN）、已批准但执行没启动，都不能报成功。
 *
 * Args:
 *   proposal: 最新的提案；核对时一次都没查到则为 null
 *   successMessage: EXECUTED 时的提示，各入口措辞不同
 */
export function describeHitlOutcome(
  proposal: HitlProposal | null,
  successMessage: string,
): HitlOutcomeNotice {
  if (proposal == null) {
    return { level: "warning", message: "暂时无法确认结果，请检查网络后刷新页面" }
  }
  switch (proposal.status.trim().toUpperCase()) {
    case "EXECUTED":
      return { level: "success", message: successMessage }
    case "EXECUTING":
      return { level: "info", message: "设备仍在执行，结果稍后会自动更新" }
    case "UNKNOWN":
      return {
        level: "warning",
        message: "执行结果不确定：命令可能已在设备上生效，请人工核实后处置",
      }
    case "APPROVED": {
      const reason =
        proposal.execution_error || readLastError(proposal.action_payload)
      return {
        level: "warning",
        message: reason
          ? `已批准但未执行：${reason}。可重试执行`
          : "已批准但未执行，可重试执行",
      }
    }
    case "REJECTED":
      return { level: "info", message: "该提案已被拒绝" }
    default:
      return { level: "warning", message: "审批尚未生效，请稍后刷新确认" }
  }
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
