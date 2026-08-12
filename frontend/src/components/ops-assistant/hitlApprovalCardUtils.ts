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
 * 批准 device_query 时是否需输入动态凭据密码。
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
  return actionType === "device_query" && assetCredentialType === "dynamic"
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
