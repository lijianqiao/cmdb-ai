/** 子 Agent 只读状态卡片

 * 展示 spawn 子任务的生命周期进度；不提供创建、等待或关闭操作。
 * WS child_status 与会话快照 children 共用同一套安全字段。
 */

import { Badge } from "@/components/ui/badge"
import { Spinner } from "@/components/ui/spinner"

import { statusLabel } from "./childAgentStatusUtils"

export interface ChildAgentStatusCardProps {
  childId: string
  role: string
  taskBrief: string
  status: string
  resultSummary: string | null
}

/**
 * 子 Agent 只读状态卡片。
 *
 * Args:
 *   props: 安全摘要字段（不含凭据与 artifacts）
 *
 * Returns:
 *   展示角色、任务简述与进度/结果的卡片
 */
export function ChildAgentStatusCard(props: ChildAgentStatusCardProps) {
  const running = ["REQUESTED", "SPAWNING", "RUNNING"].includes(
    props.status.toUpperCase(),
  )
  return (
    <div
      data-testid={`child-agent-${props.childId}`}
      className="rounded-xl border bg-card p-3 text-card-foreground"
    >
      <Badge variant="secondary">{props.role}</Badge>
      <p className="mt-2 text-sm">{props.taskBrief}</p>
      {running ? (
        <div className="mt-2 flex items-center gap-1 text-xs text-muted-foreground">
          <Spinner className="size-3" />
          <span>{statusLabel(props.status)}</span>
        </div>
      ) : (
        <p className="mt-2 text-sm text-muted-foreground">
          {props.resultSummary ?? statusLabel(props.status)}
        </p>
      )}
    </div>
  )
}
