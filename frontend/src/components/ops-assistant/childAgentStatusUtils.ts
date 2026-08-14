/** 子 Agent 状态展示纯函数（非 React 组件） */

/**
 * 将子 Agent 终态映射为中文展示文案。
 *
 * Args:
 *   status: 子任务状态（大小写不敏感）
 *
 * Returns:
 *   中文状态标签
 */
export function statusLabel(status: string): string {
  switch (status.trim().toUpperCase()) {
    case "REQUESTED":
      return "已请求"
    case "SPAWNING":
      return "启动中"
    case "RUNNING":
      return "执行中"
    case "COMPLETED":
      return "已完成"
    case "FAILED":
      return "执行失败"
    case "CANCELLED":
      return "已取消"
    case "CLOSED":
      return "已关闭"
    default:
      return status
  }
}
