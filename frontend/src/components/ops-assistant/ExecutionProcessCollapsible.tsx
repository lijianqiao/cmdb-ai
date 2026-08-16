/** 思考与执行过程折叠组件
 *
 * 聚合展示一轮对话中的全部中间过程（中间思考文本、工具调用、子 Agent、HITL 审批卡片）。
 * 在回答生成/审批阶段默认展开显示，在生成最终回答后自动折叠，支持点击图标展开/收起。
 */

import { useState } from "react"

import { HitlApprovalCard } from "@/components/ops-assistant/HitlApprovalCard"
import { ChildAgentStatusCard } from "@/components/ops-assistant/ChildAgentStatusCard"
import { ChatMarkdown } from "@/components/ops-assistant/ChatMarkdown"
import { Brain02Icon, ChevronDownIcon, ChevronUpIcon, Task01Icon } from "@/lib/icons"
import { Badge } from "@/components/ui/badge"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"
import type { OpsChatItem } from "@/hooks/use-ops-chat"

export interface ExecutionProcessCollapsibleProps {
  sessionId: number
  items: OpsChatItem[]
  isGenerating?: boolean
  className?: string
}

/**
 * 汇总执行过程中的条目数量标签
 */
function getProcessSummary(items: OpsChatItem[], isGenerating: boolean): string {
  const toolCount = items.filter((i) => i.kind === "tool_call").length
  const childCount = items.filter((i) => i.kind === "child").length
  const hitlCount = items.filter((i) => i.kind === "hitl").length

  const parts: string[] = []
  if (toolCount > 0) parts.push(`${toolCount} 个工具`)
  if (childCount > 0) parts.push(`${childCount} 个子任务`)
  if (hitlCount > 0) parts.push(`${hitlCount} 次审批`)

  if (parts.length === 0) {
    return isGenerating ? "正在思考与执行..." : "思考与执行过程"
  }

  return isGenerating
    ? `正在执行 (${parts.join(" · ")})`
    : `思考与执行过程 (${parts.join(" · ")})`
}

export function ExecutionProcessCollapsible({
  sessionId,
  items,
  isGenerating = false,
  className,
}: ExecutionProcessCollapsibleProps) {
  // 用户手动覆盖的展开状态（null 表示跟随 isGenerating 默认逻辑）
  const [manualOpen, setManualOpen] = useState<boolean | null>(null)

  if (items.length === 0) return null

  const isOpen = manualOpen ?? isGenerating
  const summaryText = getProcessSummary(items, isGenerating)

  return (
    <div
      className={cn(
        "my-1.5 overflow-hidden rounded-xl border border-border/70 bg-muted/30 transition-all",
        className,
      )}
    >
      {/* 头部摘要栏 / 折叠切换按钮 */}
      <button
        type="button"
        onClick={() => setManualOpen(!isOpen)}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-xs text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
      >
        <div className="flex items-center gap-2">
          {isGenerating ? (
            <Spinner className="size-3.5 text-primary" />
          ) : (
            <Brain02Icon className="size-3.5 text-muted-foreground" />
          )}
          <span className="font-medium">{summaryText}</span>
        </div>

        <div className="flex items-center gap-1 text-[11px]">
          <span>{isOpen ? "收起" : "展开详情"}</span>
          {isOpen ? (
            <ChevronUpIcon className="size-3.5" />
          ) : (
            <ChevronDownIcon className="size-3.5" />
          )}
        </div>
      </button>

      {/* 展开后的执行过程内容 */}
      {isOpen ? (
        <div className="flex flex-col gap-2.5 border-t border-border/50 bg-background/60 p-3">
          {items.map((item) => {
            switch (item.kind) {
              case "assistant":
                return (
                  <div
                    key={item.id}
                    className="rounded-lg border border-border/60 bg-card/70 px-3 py-2 text-xs text-card-foreground shadow-2xs"
                  >
                    <ChatMarkdown content={item.content} />
                  </div>
                )

              case "tool_call":
                return (
                  <div key={item.id} className="flex items-center gap-2">
                    <Badge
                      variant="secondary"
                      className="font-normal text-xs text-muted-foreground"
                    >
                      <Task01Icon className="mr-1 size-3" />
                      工具调用 · {item.name}
                    </Badge>
                  </div>
                )

              case "hitl":
                return (
                  <div key={`${sessionId}-${item.id}`} className="w-full max-w-md">
                    <HitlApprovalCard
                      sessionId={sessionId}
                      proposalId={item.proposalId}
                      actionType={item.actionType}
                      status={item.status}
                      reason={item.reason}
                      assetId={item.assetId}
                      resultExcerpt={item.resultExcerpt}
                      hasFullResult={item.hasFullResult}
                    />
                  </div>
                )

              case "child":
                return (
                  <div key={item.id} className="w-full max-w-md">
                    <ChildAgentStatusCard
                      childId={item.childId}
                      role={item.role}
                      taskBrief={item.taskBrief}
                      status={item.status}
                      resultSummary={item.resultSummary}
                    />
                  </div>
                )

              case "error":
                return (
                  <div
                    key={item.id}
                    className="max-w-[85%] rounded-lg border border-destructive/30 bg-destructive/5 px-2.5 py-1.5 text-xs text-destructive"
                  >
                    {item.message}
                  </div>
                )

              default:
                return null
            }
          })}
        </div>
      ) : null}
    </div>
  )
}
