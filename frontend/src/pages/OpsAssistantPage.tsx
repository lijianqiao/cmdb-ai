/** 运维助手 Chat 页

 * 左侧会话列表 + 右侧消息/输入；会话 REST + useOpsChat（含 WS）。
 * 有 knowledge:upload 时展示知识库上传入口；会话可硬删除。
 */

import { useCallback, useEffect, useState } from "react"
import dayjs from "dayjs"
import { toast } from "sonner"

import {
  AiChat01Icon,
  Delete02Icon,
  PanelLeftIcon,
  PlusSignIcon,
} from "@/lib/icons"
import { ChatInput } from "@/components/ops-assistant/ChatInput"
import { ChatMessageList } from "@/components/ops-assistant/ChatMessageList"
import { MonitorAlertBanner } from "@/components/ops-assistant/MonitorAlertBanner"
import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { HitlApprovalDialog } from "@/components/ops-assistant/HitlApprovalDialog"
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
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useOpsChat } from "@/hooks/use-ops-chat"
import {
  createAgentSession,
  deleteAgentSession,
  listAgentSessions,
  patchAgentSession,
} from "@/lib/agent-api"
import { decideHitlProposal } from "@/lib/hitl-api"
import { readErrorMessage } from "@/components/ops-assistant/hitlApprovalCardUtils"
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
 * 运维助手页面：类 Claude 全屏工作台布局，支持侧边栏折叠与宽屏问答。
 */
export function OpsAssistantPage() {
  const [sessions, setSessions] = useState<AgentSession[]>([])
  const [sessionsLoading, setSessionsLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [selectedSessionId, setSelectedSessionId] = useState<number | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [deleteTarget, setDeleteTarget] = useState<AgentSession | null>(null)
  const [fullAccessTargetSessionId, setFullAccessTargetSessionId] =
    useState<number | null>(null)
  const [patchingApprovalMode, setPatchingApprovalMode] = useState(false)

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
    cancelTurn,
    reloadSnapshot,
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

  const [dismissedProposalIds, setDismissedProposalIds] = useState<Set<number>>(
    () => new Set(),
  )

  useEffect(() => {
    setFullAccessTargetSessionId(null)
  }, [selectedSessionId])

  const pendingHitlList = messages.filter(
    (m): m is Extract<typeof messages[number], { kind: "hitl" }> =>
      m.kind === "hitl" && m.status.trim().toUpperCase() === "PENDING",
  )
  const latestPendingHitl = pendingHitlList[pendingHitlList.length - 1] ?? null

  const activeHitl =
    latestPendingHitl && !dismissedProposalIds.has(latestPendingHitl.proposalId)
      ? latestPendingHitl
      : null

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

  const [isExecutingHitl, setIsExecutingHitl] = useState(false)
  const isBusy = isSending || isExecutingHitl

  const handleApproveHitl = async (dynamicPassword?: string): Promise<void> => {
    if (activeHitl == null) return
    setIsExecutingHitl(true)
    try {
      const body: { approve: true; dynamic_credential_password?: string } = {
        approve: true,
      }
      if (dynamicPassword) {
        body.dynamic_credential_password = dynamicPassword
      }
      const updated = await decideHitlProposal(activeHitl.proposalId, body)
      toast.success(
        updated.status.trim().toUpperCase() === "APPROVED" && !updated.executed_at
          ? "已批准但未执行"
          : "审批完成，正在执行与生成回答...",
      )
    } catch (error: unknown) {
      toast.error(readErrorMessage(error, "批准失败"))
    } finally {
      setIsExecutingHitl(false)
      await reloadSnapshot()
    }
  }

  const handleRejectHitl = async (): Promise<boolean> => {
    if (activeHitl == null) return false
    setIsExecutingHitl(true)
    try {
      await decideHitlProposal(activeHitl.proposalId, { approve: false })
      toast.success("已拒绝该提案")
      return true
    } catch (error: unknown) {
      toast.error(readErrorMessage(error, "拒绝失败"))
      return false
    } finally {
      setIsExecutingHitl(false)
      await reloadSnapshot()
    }
  }

  const connectionLabel = wsStatusLabel(reconnecting, wsStatus)

  return (
    <div className="flex h-[calc(100svh-5.75rem)] md:h-[calc(100svh-6.75rem)] overflow-hidden rounded-xl border bg-background shadow-2xs">
      <h1 className="sr-only">运维助手</h1>
      {activeHitl && selectedSessionId != null ? (
        <HitlApprovalDialog
          open={true}
          onOpenChange={(open) => {
            if (!open) {
              setDismissedProposalIds(
                (prev) => new Set([...prev, activeHitl.proposalId]),
              )
            }
          }}
          sessionId={selectedSessionId}
          proposalId={activeHitl.proposalId}
          actionType={activeHitl.actionType}
          status={activeHitl.status}
          reason={activeHitl.reason}
          assetId={activeHitl.assetId}
          resultExcerpt={activeHitl.resultExcerpt}
          onApprove={handleApproveHitl}
          onReject={handleRejectHitl}
        />
      ) : null}

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
              className="bg-destructive text-white hover:bg-destructive/90 focus-visible:ring-destructive/30"
              onClick={() => void handleFullAccessConfirm()}
            >
              {patchingApprovalMode ? <Spinner data-icon="inline-start" /> : null}
              确认
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 左侧会话侧边栏（可展开/折叠） */}
      {sidebarOpen && (
        <aside className="flex w-72 shrink-0 flex-col border-r bg-card/40">
          <div className="flex items-center gap-2 p-3 pb-2">
            <Button
              type="button"
              className="flex-1 justify-start gap-2 font-medium shadow-2xs"
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
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-9 shrink-0 text-muted-foreground hover:text-foreground"
              onClick={() => setSidebarOpen(false)}
              title="收起会话列表"
            >
              <PanelLeftIcon className="size-4.5" />
            </Button>
          </div>

          <div className="flex items-center justify-between px-3.5 py-1.5 text-xs font-medium text-muted-foreground">
            <span>会话列表</span>
            {sessionsLoading ? (
              <Spinner className="size-3" />
            ) : (
              <span className="rounded-full bg-muted px-1.5 py-0.2 text-[11px]">
                {sessions.length}
              </span>
            )}
          </div>

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
                    点击上方「新建会话」开始对话。
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
                        "group flex items-stretch gap-0.5 rounded-lg transition-colors",
                        active
                          ? "bg-muted font-medium text-foreground"
                          : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                      )}
                    >
                      <Button
                        type="button"
                        variant="ghost"
                        className={cn(
                          "h-auto min-w-0 flex-1 flex-col items-start gap-0.5 px-3 py-2 text-left hover:bg-transparent",
                          active && "font-medium text-foreground",
                        )}
                        onClick={() => setSelectedSessionId(session.id)}
                      >
                        <span className="w-full truncate text-xs font-medium">
                          {session.title || `会话 #${session.id}`}
                        </span>
                        <span className="text-[11px] font-normal text-muted-foreground">
                          {dayjs(session.updated_at).format("MM-DD HH:mm")}
                          {" · "}
                          {APPROVAL_MODE_LABELS[session.approval_mode]}
                        </span>
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        className="mr-1 mt-1 shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100"
                        aria-label={`删除会话 ${session.title || session.id}`}
                        onClick={(event) => {
                          event.stopPropagation()
                          setDeleteTarget(session)
                        }}
                      >
                        <Delete02Icon className="size-3.5" />
                      </Button>
                    </div>
                  )
                })}
              </div>
            )}
          </ScrollArea>
        </aside>
      )}

      {/* 右侧主聊天区 */}
      <section className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background">
        {/* 顶部标题与状态条 */}
        <div className="flex h-12 shrink-0 items-center justify-between gap-3 border-b bg-background px-4">
          <div className="flex min-w-0 items-center gap-2.5">
            {!sidebarOpen && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="size-8.5 shrink-0 text-muted-foreground hover:text-foreground"
                onClick={() => setSidebarOpen(true)}
                title="展开会话列表"
              >
                <PanelLeftIcon className="size-4.5" />
              </Button>
            )}
            <AiChat01Icon className="size-4 shrink-0 text-primary" />
            <span className="truncate text-sm font-semibold text-foreground">
              {selectedSessionId == null
                ? "运维助手"
                : sessions.find((row) => row.id === selectedSessionId)
                    ?.title || `会话 #${selectedSessionId}`}
            </span>
            {selectedSession != null && (
              <Badge
                variant="outline"
                className="hidden text-[11px] font-normal sm:inline-flex"
              >
                {APPROVAL_MODE_LABELS[selectedSession.approval_mode]}
              </Badge>
            )}
          </div>
          <div className="flex items-center gap-2">
            {selectedSessionId != null && connectionLabel ? (
              <Badge
                variant={
                  connectionLabel === "已连接" ? "secondary" : "outline"
                }
                className="text-xs"
              >
                {(reconnecting || connectionLabel === "重连中") && (
                  <Spinner className="size-3" />
                )}
                {connectionLabel}
              </Badge>
            ) : null}
          </div>
        </div>

        {selectedSessionId == null ? (
          <div className="flex flex-1 items-center justify-center p-6">
            <div className="flex max-w-md flex-col items-center text-center">
              <div className="mb-4 flex size-14 items-center justify-center rounded-2xl bg-primary/10 text-primary [&_svg]:size-7">
                <AiChat01Icon />
              </div>
              <h2 className="text-lg font-semibold text-foreground">
                开启运维智能对话
              </h2>
              <p className="mt-1.5 text-sm text-muted-foreground">
                查询交换机与路由器配置、核查端口状态、分析告警根因与执行运维诊断。
              </p>
              <Button
                type="button"
                className="mt-5 gap-2"
                onClick={() => void handleCreateSession()}
                disabled={creating}
              >
                {creating ? <Spinner /> : <PlusSignIcon />}
                新建对话
              </Button>
            </div>
          </div>
        ) : (
          <>
            <ChatMessageList
              sessionId={selectedSessionId}
              messages={messages}
              isLoading={isLoadingHistory}
              isSending={isBusy}
              hasMore={hasMore}
              isLoadingOlder={isLoadingOlder}
              onLoadOlder={loadOlder}
              className="min-h-0 flex-1"
            />
            <div className="border-t bg-background/95 p-3 backdrop-blur-xs">
              <div className="mx-auto flex max-w-4xl flex-col gap-2">
                <MonitorAlertBanner
                  alert={monitorAlert}
                  onDismiss={clearMonitorAlert}
                  onInvestigate={sendMessage}
                  investigateDisabled={
                    inputDisabled || isExecutingHitl || isBusy
                  }
                />
                <ChatInput
                  disabled={inputDisabled || isExecutingHitl}
                  isSending={isBusy}
                  approvalMode={approvalMode}
                  onApprovalModeSelect={handleApprovalModeSelect}
                  onSend={sendMessage}
                  // HITL 执行中不给停止入口：那一轮不是 chat turn，停不掉
                  onCancel={isExecutingHitl ? undefined : cancelTurn}
                />
              </div>
            </div>
          </>
        )}
      </section>
    </div>
  )
}
