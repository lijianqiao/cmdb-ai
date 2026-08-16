/** 运维助手消息时间线
 *
 * 按对话轮次（Turn）渲染：
 * 1. 用户提问气泡（支持折叠 5 行，点击展开全部；支持复制）
 * 2. 中间思考与执行过程（中间文字、工具调用、子 Agent、HITL 审批；生成中展开，生成完毕自动折叠）
 * 3. 助手最终回答（Markdown 渲染；支持粘性悬浮复制及底部复制按钮）
 */

import { useEffect, useRef, useState } from "react"

import {
  groupMessagesIntoTurns,
  type ChatTurnGroup,
} from "@/components/ops-assistant/chatMessageUtils"
import { ExecutionProcessCollapsible } from "@/components/ops-assistant/ExecutionProcessCollapsible"
import { CopyButton } from "@/components/ops-assistant/CopyButton"
import { ChatMarkdown } from "@/components/ops-assistant/ChatMarkdown"
import { BubbleChatIcon, ChevronDownIcon, ChevronUpIcon } from "@/lib/icons"
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
  sessionId: number
  messages: OpsChatItem[]
  isLoading?: boolean
  hasMore?: boolean
  isLoadingOlder?: boolean
  onLoadOlder?: () => void
  className?: string
}

/**
 * 用户提问气泡组件（长提问最多展示 5 行，点击展开全文）
 */
function UserMessageBubble({ content }: { content: string }) {
  const [expanded, setExpanded] = useState(false)
  const isLong = content.split("\n").length > 5 || content.length > 220

  return (
    <div className="group relative flex justify-end">
      <div
        role={isLong ? "button" : undefined}
        tabIndex={isLong ? 0 : undefined}
        onClick={() => {
          if (isLong) setExpanded(!expanded)
        }}
        onKeyDown={(e) => {
          if (isLong && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault()
            setExpanded(!expanded)
          }
        }}
        className={cn(
          "relative max-w-[85%] rounded-2xl bg-muted px-3.5 py-2.5 text-sm text-foreground transition-all",
          isLong && "cursor-pointer select-text hover:bg-muted/90",
        )}
      >
        {/* 右上角粘性悬浮复制提问按钮（向下滚动时长提问复制按钮始终吸附在右上角） */}
        <div className="sticky top-2 float-right z-10 -mr-1 -mt-1 ml-2 mb-2 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
          <CopyButton
            text={content}
            label="复制提问内容"
            successMessage="已复制提问内容"
            className="size-6 rounded-md bg-background/90 shadow-xs backdrop-blur-xs"
          />
        </div>

        {/* 提问正文（超出 5 行时根据 expanded 状态截断） */}
        <p
          className={cn(
            "whitespace-pre-wrap break-words",
            isLong && !expanded && "line-clamp-5",
          )}
        >
          {content}
        </p>

        {/* 长文本展开/收起提示 */}
        {isLong ? (
          <div className="mt-1 flex items-center justify-end gap-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground">
            <span>{expanded ? "点击收起" : "点击展开全文"}</span>
            {expanded ? (
              <ChevronUpIcon className="size-3" />
            ) : (
              <ChevronDownIcon className="size-3" />
            )}
          </div>
        ) : null}
      </div>
    </div>
  )
}

/**
 * 单个问答轮次渲染
 */
function TurnRow({
  turn,
  sessionId,
  isLastTurn,
}: {
  turn: ChatTurnGroup
  sessionId: number
  isLastTurn: boolean
}) {
  const isGenerating = Boolean(
    turn.assistantMessage?.streaming ||
      (!turn.assistantMessage && isLastTurn && turn.processItems.length > 0),
  )

  return (
    <div className="flex flex-col gap-2.5">
      {/* 1. 用户提问气泡（支持折叠 5 行，点击展开） */}
      {turn.userMessage ? (
        <UserMessageBubble content={turn.userMessage.content} />
      ) : null}

      {/* 2. 中间思考与执行过程（中间文字、工具调用、子 Agent、HITL 审批全部折叠在此） */}
      {turn.processItems.length > 0 ? (
        <div className="flex justify-start">
          <div className="w-full max-w-[90%] md:max-w-2xl">
            <ExecutionProcessCollapsible
              sessionId={sessionId}
              items={turn.processItems}
              isGenerating={isGenerating}
            />
          </div>
        </div>
      ) : null}

      {/* 3. 助手最终回答（全轮唯一暴露在外的最终结果） */}
      {turn.assistantMessage ? (
        <div className="group relative flex justify-start">
          <div className="relative max-w-[90%] rounded-2xl border bg-card px-4 py-3 text-card-foreground shadow-xs md:max-w-3xl">
            {/* 右上角粘性悬浮复制按钮（长回答滚动时始终吸附在右上角可视区域内） */}
            {turn.assistantMessage.content ? (
              <div className="sticky top-2 float-right z-10 -mr-1 -mt-1 ml-2 mb-2">
                <CopyButton
                  text={turn.assistantMessage.content}
                  label="复制回答内容"
                  successMessage="已复制回答内容 (Markdown)"
                  className="size-7 rounded-md border border-border/50 bg-card/95 shadow-xs backdrop-blur-xs hover:bg-muted"
                />
              </div>
            ) : null}

            {/* Markdown 正文 */}
            {turn.assistantMessage.content ? (
              <div className="text-sm">
                <ChatMarkdown content={turn.assistantMessage.content} />
              </div>
            ) : turn.assistantMessage.streaming ? (
              <p className="text-sm text-muted-foreground">…</p>
            ) : null}

            {/* 流式生成中动画 */}
            {turn.assistantMessage.streaming ? (
              <div className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
                <Spinner className="size-3.5 text-primary" />
                <span>生成中...</span>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      {/* 4. 错误信息 */}
      {turn.errors.map((err) => (
        <div key={err.id} className="flex justify-start">
          <div className="max-w-[85%] rounded-2xl border border-destructive/30 bg-destructive/5 px-3.5 py-2 text-sm text-destructive">
            {err.message}
          </div>
        </div>
      ))}
    </div>
  )
}

/**
 * 聊天消息列表（ScrollArea）；新消息到达时滚到底部，向上分页时保持滚动位置。
 */
export function ChatMessageList({
  sessionId,
  messages,
  isLoading = false,
  hasMore = false,
  isLoadingOlder = false,
  onLoadOlder,
  className,
}: ChatMessageListProps) {
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const topSentinelRef = useRef<HTMLDivElement | null>(null)
  const scrollRootRef = useRef<HTMLDivElement | null>(null)
  const prevCountRef = useRef(messages.length)
  const prevFirstIdRef = useRef(messages[0]?.id)

  useEffect(() => {
    const scrollRoot = scrollRootRef.current
    const node = topSentinelRef.current
    if (!scrollRoot || !node || !hasMore || isLoadingOlder || !onLoadOlder) return

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) void onLoadOlder()
      },
      { root: scrollRoot },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [hasMore, isLoadingOlder, onLoadOlder, messages.length])

  useEffect(() => {
    const firstId = messages[0]?.id
    const prepended =
      messages.length > prevCountRef.current && firstId !== prevFirstIdRef.current
    const scrollRoot = scrollRootRef.current
    const prevScrollHeight = scrollRoot?.scrollHeight ?? 0
    const prevScrollTop = scrollRoot?.scrollTop ?? 0

    prevCountRef.current = messages.length
    prevFirstIdRef.current = firstId

    if (prepended && scrollRoot) {
      requestAnimationFrame(() => {
        const delta = scrollRoot.scrollHeight - prevScrollHeight
        scrollRoot.scrollTop = prevScrollTop + delta
      })
      return
    }

    if (!prepended) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })
    }
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

  const turns = groupMessagesIntoTurns(messages)

  return (
    <ScrollArea ref={scrollRootRef} className={cn("bg-background", className)}>
      <div className="flex flex-col gap-4 p-4">
        <div ref={topSentinelRef} className="h-px w-full shrink-0" />
        {isLoadingOlder ? (
          <div className="flex justify-center py-2">
            <Spinner className="size-4 text-muted-foreground" />
          </div>
        ) : null}
        {turns.map((turn, index) => (
          <TurnRow
            key={`${sessionId}-${turn.id}`}
            turn={turn}
            sessionId={sessionId}
            isLastTurn={index === turns.length - 1}
          />
        ))}
        <div ref={bottomRef} />
      </div>
    </ScrollArea>
  )
}
