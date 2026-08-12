/** 运维助手消息时间线

 * 渲染 useOpsChat 的 OpsChatItem：用户/助手气泡、tool_call Badge、HITL 插槽、错误行。
 * HitlApprovalCard 由 Task 8 接入，此处仅占位展示安全摘要。
 */

import { useEffect, useRef } from "react"

import { BubbleChatIcon } from "@/lib/icons"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"
import type { OpsChatItem } from "@/hooks/use-ops-chat"

export interface ChatMessageListProps {
  messages: OpsChatItem[]
  isLoading?: boolean
  className?: string
}

/**
 * HITL 审批卡片占位（Task 8 替换为 HitlApprovalCard）。
 *
 * Args:
 *   item: hitl 时间线条目（仅安全摘要字段）
 */
function HitlApprovalSlot({
  item,
}: {
  item: Extract<OpsChatItem, { kind: "hitl" }>
}) {
  return (
    <Card className="border-dashed bg-card">
      <CardContent className="flex flex-col gap-2 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">待审批</Badge>
          <Badge variant="secondary">{item.status || "pending"}</Badge>
          {item.actionType ? (
            <span className="text-sm text-muted-foreground">
              {item.actionType}
            </span>
          ) : null}
        </div>
        {item.reason ? (
          <p className="text-sm text-foreground">{item.reason}</p>
        ) : null}
        {item.assetId != null ? (
          <p className="text-xs text-muted-foreground">资产 #{item.assetId}</p>
        ) : null}
        <p className="text-xs text-muted-foreground">
          审批操作将在后续版本启用（HitlApprovalCard）
        </p>
      </CardContent>
    </Card>
  )
}

/**
 * 单条时间线渲染。
 *
 * Args:
 *   item: OpsChatItem
 */
function MessageRow({ item }: { item: OpsChatItem }) {
  switch (item.kind) {
    case "user":
      return (
        <div className="flex justify-end">
          <div className="max-w-[85%] rounded-2xl bg-muted px-3 py-2 text-sm text-foreground">
            <p className="whitespace-pre-wrap break-words">{item.content}</p>
          </div>
        </div>
      )

    case "assistant":
      return (
        <div className="flex justify-start">
          <div className="max-w-[85%] rounded-2xl border bg-card px-3 py-2 text-sm text-card-foreground">
            <p className="whitespace-pre-wrap break-words">
              {item.content || (item.streaming ? "…" : "")}
            </p>
            {item.streaming ? (
              <div className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
                <Spinner className="size-3" />
                <span>生成中</span>
              </div>
            ) : null}
          </div>
        </div>
      )

    case "tool_call":
      return (
        <div className="flex justify-start">
          <Badge variant="secondary">工具调用 · {item.name}</Badge>
        </div>
      )

    case "hitl":
      return (
        <div className="flex justify-start">
          <div className="w-full max-w-md">
            <HitlApprovalSlot item={item} />
          </div>
        </div>
      )

    case "error":
      return (
        <div className="flex justify-start">
          <div className="max-w-[85%] rounded-2xl border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            {item.message}
          </div>
        </div>
      )

    default:
      return null
  }
}

/**
 * 聊天消息列表（ScrollArea）；新消息到达时滚到底部。
 *
 * Args:
 *   messages: 时间线条目
 *   isLoading: 历史加载中显示 Skeleton
 *   className: 外层布局 class
 */
export function ChatMessageList({
  messages,
  isLoading = false,
  className,
}: ChatMessageListProps) {
  const bottomRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })
  }, [messages])

  if (isLoading) {
    return (
      <div className={cn("flex flex-col gap-3 bg-background p-4", className)}>
        <Skeleton className="h-10 w-2/3 self-end rounded-2xl" />
        <Skeleton className="h-16 w-3/4 rounded-2xl" />
        <Skeleton className="h-10 w-1/2 self-end rounded-2xl" />
        <Skeleton className="h-12 w-2/3 rounded-2xl" />
      </div>
    )
  }

  if (messages.length === 0) {
    return (
      <div
        className={cn(
          "flex items-center justify-center bg-background p-4",
          className,
        )}
      >
        <Empty className="border-0">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <BubbleChatIcon />
            </EmptyMedia>
            <EmptyTitle>开始对话</EmptyTitle>
            <EmptyDescription>
              在下方输入运维问题，助手会结合工具与知识库回复。
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      </div>
    )
  }

  return (
    <ScrollArea className={cn("bg-background", className)}>
      <div className="flex flex-col gap-3 p-4">
        {messages.map((item) => (
          <MessageRow key={item.id} item={item} />
        ))}
        <div ref={bottomRef} />
      </div>
    </ScrollArea>
  )
}
