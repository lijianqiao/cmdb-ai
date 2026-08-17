/** 监控告警 → 排查请求的文案构造
 *
 * 单独成文件而不是放进 MonitorAlertBanner.tsx：组件文件只导出组件，
 * 否则 Vite Fast Refresh 会失效（react-refresh/only-export-components）。
 * 与 monitor/monitorTargetFormSchema.ts 的拆法保持一致。
 */

function readText(value: unknown): string {
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value)
  }
  return ""
}

/**
 * 把告警 payload 拼成一句给运维助手的排查请求。
 *
 * 刻意写成自然语言而不是工具调用参数：根 Agent 需要自己判断用
 * investigate_root_cause 还是直接查——把已知事实（IP、端口、状态翻转、
 * 时间）给足，剩下的交给它，而不是替它决定。
 *
 * 字段缺失时整段略去，不留「名称：」这种空标签，避免助手把空值当成
 * 有意义的输入去追问。
 *
 * Args:
 *   alert: WS monitor_alert 的 payload
 *
 * Returns:
 *   可直接作为用户消息发送的中文请求
 */
export function buildInvestigationPrompt(
  alert: Record<string, unknown>,
): string {
  const facts: string[] = []
  const label = readText(alert.asset_name)
  const ip = readText(alert.ip_address)
  const port = readText(alert.port)
  const previous = readText(alert.previous_status)
  const current = readText(alert.status)
  const checkedAt = readText(alert.checked_at)
  const targetId = readText(alert.target_id)
  const assetId = readText(alert.asset_id)

  if (label) facts.push(`名称：${label}`)
  if (ip) facts.push(`地址：${ip}${port ? `:${port}` : ""}`)
  if (previous && current) facts.push(`状态：${previous} → ${current}`)
  else if (current) facts.push(`状态：${current}`)
  if (checkedAt) facts.push(`探测时间：${checkedAt}`)
  if (targetId) facts.push(`监控目标 ID：${targetId}`)
  if (assetId) facts.push(`CMDB 资产 ID：${assetId}`)

  const detail = facts.length > 0 ? `\n${facts.join("\n")}` : ""
  return (
    `请排查这条监控告警的原因。${detail}\n\n` +
    "请并行核查：监控历史是否有同类抖动、该资产的 CMDB 归属与上下游依赖、" +
    "同网段或同业务系统的其它资产是否有相同现象。最后给出根因假设与下一步建议。"
  )
}
