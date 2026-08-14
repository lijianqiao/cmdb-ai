/** HitlApprovalCard 纯函数：执行结果展示与动态凭据校验 */

/**
 * 是否应在卡片上展示执行结果摘要。
 *
 * Args:
 *   status: 当前展示状态
 *   resultExcerpt: HTTP 详情或 WS 摘要中的结果片段
 *
 * Returns:
 *   为 true 时表示应渲染结果区
 */
export function shouldShowResultExcerpt(
  status: string,
  resultExcerpt: string | null | undefined,
): boolean {
  if (status.trim().toUpperCase() !== "EXECUTED") return false
  return typeof resultExcerpt === "string" && resultExcerpt.trim().length > 0
}

/**
 * 批准或重试设备命令时是否需输入动态凭据密码。
 *
 * Args:
 *   actionType: 动作类型
 *   assetCredentialType: 目标资产凭据类型（仅类型，非密码）
 *
 * Returns:
 *   为 true 时需密码输入框
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
 * 批准按钮是否应禁用（含动态密码必填校验）。
 *
 * Args:
 *   deciding: 正在提交审批
 *   detailLoading: 详情加载中
 *   needsPassword: 是否需动态密码
 *   password: 当前密码输入
 *
 * Returns:
 *   为 true 时禁用批准按钮
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
 * 从提案载荷中读取上次执行失败的分类信息。
 *
 * Args:
 *   payload: HTTP 详情里的 action_payload
 *
 * Returns:
 *   失败分类文案；无则 null
 */
export function readLastError(
  payload: Record<string, unknown> | null | undefined,
): string | null {
  const value = payload?.last_error
  return typeof value === "string" && value.trim() ? value : null
}

/**
 * 是否展示「重试执行」操作（仅 APPROVED 且有审批权限）。
 *
 * Args:
 *   canApprove: 是否持有 agent:hitl_approve
 *   status: 当前展示状态
 *
 * Returns:
 *   为 true 时展示重试按钮
 */
export function isRetryAvailable(canApprove: boolean, status: string): boolean {
  return canApprove && status.trim().toUpperCase() === "APPROVED"
}

/**
 * 是否展示 UNKNOWN 人工处置操作（仅 UNKNOWN 且有审批权限）。
 *
 * Args:
 *   canApprove: 是否持有 agent:hitl_approve
 *   status: 当前展示状态
 *
 * Returns:
 *   为 true 时展示确认已执行与允许重试按钮
 */
export function isUnknownResolutionAvailable(
  canApprove: boolean,
  status: string,
): boolean {
  return canApprove && status.trim().toUpperCase() === "UNKNOWN"
}
