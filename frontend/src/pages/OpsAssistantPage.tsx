/** 运维助手 Chat 页

 * 左侧会话列表 + 右侧消息/输入；会话 REST + useOpsChat（含 WS）。
 * 有 knowledge:upload 时展示知识库上传入口；会话可硬删除。
 */

import { useCallback, useEffect, useState } from "react"
import dayjs from "dayjs"
import { toast } from "sonner"

import { AiChat01Icon, Delete02Icon, PlusSignIcon, Upload01Icon } from "@/lib/icons"
import { ChatInput } from "@/components/ops-assistant/ChatInput"
import { ChatMessageList } from "@/components/ops-assistant/ChatMessageList"
import { KnowledgeUploadDialog } from "@/components/ops-assistant/KnowledgeUploadDialog"
import { MonitorAlertBanner } from "@/components/ops-assistant/MonitorAlertBanner"
import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { PageHeader } from "@/components/layout/PageHeader"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useOpsChat } from "@/hooks/use-ops-chat"
import { usePermission } from "@/hooks/use-permission"
import {
  createAgentSession,
  deleteAgentSession,
  listAgentSessions,
  patchAgentSession,
} from "@/lib/agent-api"
import { PERMISSIONS } from "@/lib/constants"
import { cn } from "@/lib/utils"
import type { ApprovalMode, AgentSession } from "@/types/agent"
import { APPROVAL_MODE_LABELS } from "@/types/agent"

function wsStatusLabel(
  reconnecting: boolean,
  status: ReturnType<typeof useOpsChat>["wsStatus"],
): string | null {
  if (reconnecting || status === "reconnecting") return "重连中"
  if (status === "connecting") return "连接中"
  if (status === "open") return "已连接"
  if (status === "closed") return "已断开"
  return null
}

/**
 * 运维助手页面：会话选择、消息时间线、发送输入。
 */
export function OpsAssistantPage() {
  const { hasPermission } = usePermission()
  const [sessions, setSessions] = useState<AgentSession[]>([])
  const [sessionsLoading, setSessionsLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [selectedSessionId, setSelectedSessionId] = useState<number | null>(null)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<AgentSession | null>(null)
  const [fullAccessTargetSessionId, setFullAccessTargetSessionId] =
    useState<number | null>(null)
  const [patchingApprovalMode, setPatchingApprovalMode] = useState(false)
  const canUploadKnowledge = hasPermission(PERMISSIONS.KNOWLEDGE_UPLOAD)

  const selectedSession =
    selectedSessionId == null
      ? null
      : sessions.find((row) => row.id === selectedSessionId) ?? null
  const approvalMode = selectedSession?.approval_mode ?? null

  const {
    messages,
    isLoadingHistory,
    isSending,
    inputDisabled,
    wsStatus,
    reconnecting,
    monitorAlert,
    clearMonitorAlert,
    sendMessage,
    loadOlder,
    hasMore,
    isLoadingOlder,
  } = useOpsChat({ sessionId: selectedSessionId })

  const loadSessions = useCallback(async (preferSessionId?: number | null) => {
    setSessionsLoading(true)
    try {
      const page = await listAgentSessions({ page: 1, page_size: 50 })
      const items = page.items ?? []
      setSessions(items)
      setSelectedSessionId((current) => {
        if (preferSessionId != null) {
          const exists = items.some((row) => row.id === preferSessionId)
          if (exists) return preferSessionId
        }
        if (current != null && items.some((row) => row.id === current)) {
          return current
        }
        return items[0]?.id ?? null
      })
    } catch {
      toast.error("加载会话列表失败")
      setSessions([])
    } finally {
      setSessionsLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadSessions()
  }, [loadSessions])

  useEffect(() => {
    setFullAccessTargetSessionId(null)
  }, [selectedSessionId])

  const handleCreateSession = async (): Promise<void> => {
    if (creating) return
    setCreating(true)
    try {
      const created = await createAgentSession({})
      toast.success("已新建会话")
      await loadSessions(created.id)
    } catch {
      toast.error("新建会话失败")
    } finally {
      setCreating(false)
    }
  }

  const handleDeleteConfirm = async (): Promise<boolean> => {
    if (deleteTarget == null) return false
    const deletingId = deleteTarget.id
    try {
      await deleteAgentSession(deletingId)
      toast.success("会话已删除")
      const remaining = sessions.filter((row) => row.id !== deletingId)
      setSessions(remaining)
      setSelectedSessionId((current) => {
        if (current !== deletingId) return current
        return remaining[0]?.id ?? null
      })
      setDeleteTarget(null)
      return true
    } catch {
      toast.error("删除会话失败")
      return false
    }
  }

  const updateSessionInList = (updated: AgentSession): void => {
    setSessions((current) =>
      current.map((row) => (row.id === updated.id ? updated : row)),
    )
  }

  const patchApprovalMode = async (
    sessionId: number,
    mode: ApprovalMode,
  ): Promise<void> => {
    setPatchingApprovalMode(true)
    try {
      const updated = await patchAgentSession(sessionId, {
        approval_mode: mode,
      })
      updateSessionInList(updated)
    } catch {
      toast.error("变更审批模式失败")
    } finally {
      setPatchingApprovalMode(false)
    }
  }

  const handleApprovalModeSelect = (mode: ApprovalMode): void => {
    if (selectedSessionId == null || approvalMode == null) return
    if (mode === "full" && approvalMode !== "full") {
      setFullAccessTargetSessionId(selectedSessionId)
      return
    }
    void patchApprovalMode(selectedSessionId, mode)
  }

  const handleFullAccessConfirm = async (): Promise<void> => {
    const targetId = fullAccessTargetSessionId
    setFullAccessTargetSessionId(null)
    if (targetId == null) return
    await patchApprovalMode(targetId, "full")
  }

  const connectionLabel = wsStatusLabel(reconnecting, wsStatus)

  return (
    <div className="flex h-[calc(100svh-7.5rem)] flex-col gap-4">
      <PageHeader
        title="运维助手"
        description="通过对话查询与处理运维问题"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {canUploadKnowledge && (
              <Button
                type="button"
                variant="outline"
                onClick={() => setUploadOpen(true)}
              >
                <Upload01Icon data-icon="inline-start" />
                上传知识
              </Button>
            )}
            <Button
              type="button"
              onClick={() => void handleCreateSession()}
              disabled={creating}
            >
              {creating ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <PlusSignIcon data-icon="inline-start" />
              )}
              新建会话
            </Button>
          </div>
        }
      />

      <KnowledgeUploadDialog open={uploadOpen} onOpenChange={setUploadOpen} />

      <ConfirmDialog
        open={deleteTarget != null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null)
        }}
        title="确认删除会话"
        description={`确定要删除「${
          deleteTarget?.title || `会话 #${deleteTarget?.id ?? ""}`
        }」吗？消息与相关记录将一并永久删除，不可恢复。`}
        onConfirm={handleDeleteConfirm}
      />

      <Dialog
        open={fullAccessTargetSessionId != null}
        onOpenChange={(open) => {
          if (!open) setFullAccessTargetSessionId(null)
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>确认开启完全访问</DialogTitle>
            <DialogDescription>
              未分类的设备命令将不再询问你；黑名单仍然拒绝；动态凭据仍要你输入本次密码。此设置只对当前对话有效。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setFullAccessTargetSessionId(null)}
            >
              取消
            </Button>
            <Button
              type="button"
              disabled={patchingApprovalMode}
              onClick={() => void handleFullAccessConfirm()}
            >
              {patchingApprovalMode ? <Spinner data-icon="inline-start" /> : null}
              确认
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <div className="flex min-h-0 flex-1 flex-col gap-4 md:flex-row">
        <aside className="flex w-full shrink-0 flex-col gap-2 rounded-xl border bg-card md:w-64">
          <div className="flex items-center justify-between px-3 py-2">
            <span className="text-sm font-medium">会话</span>
            {sessionsLoading ? (
              <Spinner className="size-3.5 text-muted-foreground" />
            ) : (
              <span className="text-xs text-muted-foreground">
                {sessions.length}
              </span>
            )}
          </div>
          <Separator />
          <ScrollArea className="min-h-0 flex-1 px-2 pb-2">
            {sessionsLoading ? (
              <div className="flex flex-col gap-2 p-1">
                {Array.from({ length: 4 }).map((_, index) => (
                  <Skeleton key={index} className="h-12 w-full rounded-lg" />
                ))}
              </div>
            ) : sessions.length === 0 ? (
              <Empty className="border-0 p-6">
                <EmptyHeader>
                  <EmptyMedia variant="icon">
                    <AiChat01Icon />
                  </EmptyMedia>
                  <EmptyTitle>暂无会话</EmptyTitle>
                  <EmptyDescription>
                    点击右上角「新建会话」开始对话。
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            ) : (
              <div className="flex flex-col gap-1">
                {sessions.map((session) => {
                  const active = session.id === selectedSessionId
                  return (
                    <div
                      key={session.id}
                      className={cn(
                        "group flex items-stretch gap-0.5 rounded-lg",
                        active && "bg-muted",
                      )}
                    >
                      <Button
                        type="button"
                        variant={active ? "secondary" : "ghost"}
                        className={cn(
                          "h-auto min-w-0 flex-1 flex-col items-start gap-0.5 px-3 py-2 text-left",
                          active && "bg-transparent hover:bg-transparent",
                        )}
                        onClick={() => setSelectedSessionId(session.id)}
                      >
                        <span className="w-full truncate text-sm font-medium">
                          {session.title || `会话 #${session.id}`}
                        </span>
                        <span className="text-xs font-normal text-muted-foreground">
                          {dayjs(session.updated_at).format("MM-DD HH:mm")}
                          {" · "}
                          {APPROVAL_MODE_LABELS[session.approval_mode]}
                        </span>
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        className="mt-1 mr-1 shrink-0 text-muted-foreground opacity-70 hover:text-destructive group-hover:opacity-100"
                        aria-label={`删除会话 ${session.title || session.id}`}
                        onClick={(event) => {
                          event.stopPropagation()
                          setDeleteTarget(session)
                        }}
                      >
                        <Delete02Icon />
                      </Button>
                    </div>
                  )
                })}
              </div>
            )}
          </ScrollArea>
        </aside>

        <section className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-xl border bg-background">
          <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
            <div className="flex min-w-0 items-center gap-2">
              <AiChat01Icon className="size-4 shrink-0 text-muted-foreground" />
              <span className="truncate text-sm font-medium">
                {selectedSessionId == null
                  ? "未选择会话"
                  : sessions.find((row) => row.id === selectedSessionId)
                      ?.title || `会话 #${selectedSessionId}`}
              </span>
            </div>
            {selectedSessionId != null && connectionLabel ? (
              <Badge
                variant={
                  connectionLabel === "已连接" ? "secondary" : "outline"
                }
              >
                {(reconnecting || connectionLabel === "重连中") && (
                  <Spinner className="size-3" />
                )}
                {connectionLabel}
              </Badge>
            ) : null}
          </div>

          {selectedSessionId == null ? (
            <div className="flex flex-1 items-center justify-center p-4">
              <Empty className="border-0">
                <EmptyHeader>
                  <EmptyMedia variant="icon">
                    <AiChat01Icon />
                  </EmptyMedia>
                  <EmptyTitle>选择或新建会话</EmptyTitle>
                  <EmptyDescription>
                    左侧选择已有会话，或新建会话后开始提问。
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            </div>
          ) : (
            <>
              <ChatMessageList
                messages={messages}
                isLoading={isLoadingHistory}
                hasMore={hasMore}
                isLoadingOlder={isLoadingOlder}
                onLoadOlder={loadOlder}
                className="min-h-0 flex-1"
              />
              <div className="flex flex-col gap-2 border-t p-3">
                <MonitorAlertBanner
                  alert={monitorAlert}
                  onDismiss={clearMonitorAlert}
                />
                <ChatInput
                  disabled={inputDisabled}
                  isSending={isSending}
                  approvalMode={approvalMode}
                  onApprovalModeSelect={handleApprovalModeSelect}
                  onSend={sendMessage}
                />
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
