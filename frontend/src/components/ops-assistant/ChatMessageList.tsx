/** 运维助手消息时间线

 * 渲染 useOpsChat 的 OpsChatItem：用户/助手气泡、tool_call Badge、HITL 卡片、错误行。
 */

import { useEffect, useRef } from "react"

import { HitlApprovalCard } from "@/components/ops-assistant/HitlApprovalCard"
import { ChatMarkdown } from "@/components/ops-assistant/ChatMarkdown"
import { BubbleChatIcon } from "@/lib/icons"
import { Badge } from "@/components/ui/badge"
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
          <div className="max-w-[85%] rounded-2xl border bg-card px-3 py-2 text-card-foreground">
            {item.content ? (
              <ChatMarkdown content={item.content} />
            ) : item.streaming ? (
              <p className="text-sm text-muted-foreground">…</p>
            ) : null}
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
            <HitlApprovalCard
              proposalId={item.proposalId}
              actionType={item.actionType}
              status={item.status}
              reason={item.reason}
              assetId={item.assetId}
              resultExcerpt={item.resultExcerpt}
            />
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
