/** 运维助手会话状态 hook

 * 加载 REST 快照 →（快照 settle 后）开 WS → 本地消息列表 → POST 发送 → 合并实时事件。
 * 消息合并抽为纯 reducer；isSending 仅由 POST finally 解除（turn_done 不解锁）。
 */

import { useCallback, useEffect, useReducer, useRef, useState } from "react"
import { toast } from "sonner"

import {
  getAgentSessionSnapshot,
  postAgentMessage,
} from "@/lib/agent-api"
import { useAgentWs, type AgentWsStatus } from "@/hooks/use-agent-ws"
import type {
  AgentMessage,
  AgentSessionSnapshot,
  AgentWsServerMessage,
  ChildAgentSnapshot,
  HitlProposalSafeSummary,
} from "@/types/agent"

/**
 * 是否允许连接 Agent WS。
 * 仅在「当前 session 的快照已 settle」后为 true，避免 GET 与实时事件竞态。
 */
export function shouldEnableAgentWs(
  sessionId: number | null | undefined,
  snapshotReadySessionId: number | null,
): boolean {
  return sessionId != null && snapshotReadySessionId === sessionId
}

/**
 * HTTP catch 是否还需合成 error 行。
 * 本轮若 WS 已推送 error，则跳过，避免双行。
 */
export function shouldSynthesizeSendError(wsErrorReceived: boolean): boolean {
  return !wsErrorReceived
}

/** 统一时间线条目（历史 REST + 实时 WS） */
export type OpsChatItem =
  | {
      kind: "user"
      id: string
      content: string
      serverId?: number
      createdAt?: string
      pending?: boolean
    }
  | {
      kind: "assistant"
      id: string
      content: string
      streaming: boolean
      serverId?: number
      createdAt?: string
    }
  | {
      kind: "tool_call"
      id: string
      toolCallId: string
      name: string
    }
  | {
      kind: "hitl"
      id: string
      proposalId: number
      actionType: string
      status: string
      reason: string
      assetId: number | null
      /** WS 安全摘要中的执行结果片段（无审批权限时展示用） */
      resultExcerpt: string | null
      /** 服务端是否保存了可按需读取的完整结果 */
      hasFullResult: boolean
    }
  | {
      kind: "error"
      id: string
      message: string
    }
  | {
      kind: "child"
      id: string
      childId: string
      role: string
      taskBrief: string
      status: string
      resultSummary: string | null
    }

export interface OpsChatState {
  items: OpsChatItem[]
}

export type OpsChatAction =
  | { type: "reset" }
  | { type: "history_loaded"; messages: AgentMessage[] }
  | {
      type: "snapshot_loaded"
      snapshot: AgentSessionSnapshot
      replace: boolean
    }
  | { type: "user_sent"; clientId: string; content: string }
  | { type: "user_settled"; clientId: string }
  | { type: "ws"; message: AgentWsServerMessage }

const initialState: OpsChatState = { items: [] }

let ephemeralSeq = 0

/** 生成仅前端可见的临时 id */
function nextEphemeralId(prefix: string): string {
  ephemeralSeq += 1
  return `${prefix}-${ephemeralSeq}`
}

function messageItemId(messageId: number): string {
  return `message:${messageId}`
}

function hitlItemId(proposalId: number): string {
  return `hitl:${proposalId}`
}

function childItemId(childId: string): string {
  return `child:${childId}`
}

/**
 * 将 REST 历史行映射为时间线条目。
 *
 * Args:
 *   messages: AgentMessage 数组
 *
 * Returns:
 *   OpsChatItem 列表（跳过 role=tool 的工具结果正文）
 */
export function mapHistoryToItems(messages: AgentMessage[]): OpsChatItem[] {
  const items: OpsChatItem[] = []
  for (const row of messages) {
    if (row.role === "user") {
      items.push({
        kind: "user",
        id: messageItemId(row.id),
        serverId: row.id,
        content: row.content,
        createdAt: row.created_at,
      })
      continue
    }
    if (row.role === "assistant") {
      if (row.tool_calls?.length) {
        for (const tc of row.tool_calls) {
          const toolCallId =
            typeof tc.id === "string" ? tc.id : String(tc.id ?? "")
          const name = typeof tc.name === "string" ? tc.name : "tool"
          items.push({
            kind: "tool_call",
            id: `toolcall:${row.id}:${toolCallId || nextEphemeralId("tc")}`,
            toolCallId,
            name,
          })
        }
      }
      if (row.content) {
        items.push({
          kind: "assistant",
          id: messageItemId(row.id),
          serverId: row.id,
          content: row.content,
          streaming: false,
          createdAt: row.created_at,
        })
      }
    }
  }
  return items
}

function mapProposalToItem(
  proposal: HitlProposalSafeSummary,
): OpsChatItem {
  return {
    kind: "hitl",
    id: hitlItemId(proposal.proposal_id),
    proposalId: proposal.proposal_id,
    actionType: proposal.action_type,
    status: proposal.status,
    reason: proposal.reason,
    assetId: proposal.asset_id,
    resultExcerpt: proposal.result_excerpt,
    hasFullResult: proposal.has_full_result,
  }
}

function mapChildToItem(child: ChildAgentSnapshot): OpsChatItem {
  return {
    kind: "child",
    id: childItemId(child.child_id),
    childId: child.child_id,
    role: child.role,
    taskBrief: child.task_brief,
    status: child.status,
    resultSummary: child.result_summary,
  }
}

function mapSnapshotToItems(snapshot: AgentSessionSnapshot): OpsChatItem[] {
  const items = mapHistoryToItems(snapshot.messages)
  for (const row of snapshot.proposals) {
    items.push(mapProposalToItem(row))
  }
  for (const row of snapshot.children) {
    items.push(mapChildToItem(row))
  }
  return items
}

function mergeReplaceSnapshot(
  existing: OpsChatItem[],
  incoming: OpsChatItem[],
): OpsChatItem[] {
  const pendingUsers = existing.filter(
    (item) =>
      item.kind === "user" && item.serverId == null && item.pending === true,
  )
  const incomingIds = new Set(incoming.map((item) => item.id))
  const keptPending = pendingUsers.filter((item) => !incomingIds.has(item.id))
  return [...incoming, ...keptPending]
}

function mergePrependSnapshot(
  existing: OpsChatItem[],
  incoming: OpsChatItem[],
): OpsChatItem[] {
  const existingIds = new Set(existing.map((item) => item.id))
  const toPrepend = incoming.filter((item) => !existingIds.has(item.id))
  return [...toPrepend, ...existing]
}

function readString(payload: Record<string, unknown>, key: string): string {
  const value = payload[key]
  return typeof value === "string" ? value : ""
}

function readProposalId(payload: Record<string, unknown>): number | null {
  const value = payload.proposal_id
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function readAssetId(payload: Record<string, unknown>): number | null {
  const value = payload.asset_id
  if (value == null) return null
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function readResultExcerpt(payload: Record<string, unknown>): string | null {
  const value = payload.result_excerpt
  return typeof value === "string" ? value : null
}

function readHasFullResult(payload: Record<string, unknown>): boolean {
  return payload.has_full_result === true
}

function readNullableString(
  payload: Record<string, unknown>,
  key: string,
): string | null {
  const value = payload[key]
  return typeof value === "string" ? value : null
}

function mapChildStatusPayload(
  payload: Record<string, unknown>,
): Extract<OpsChatItem, { kind: "child" }> | null {
  const childId = readString(payload, "child_id")
  if (!childId) return null
  return {
    kind: "child",
    id: childItemId(childId),
    childId,
    role: readString(payload, "role"),
    taskBrief: readString(payload, "task_brief"),
    status: readString(payload, "status"),
    resultSummary: readNullableString(payload, "result_summary"),
  }
}

function mergeChildStatusItem(
  items: OpsChatItem[],
  incoming: Extract<OpsChatItem, { kind: "child" }>,
): OpsChatItem[] {
  let replaced = false
  const next = items.map((item) => {
    if (item.kind !== "child") return item
    if (item.childId !== incoming.childId) return item
    replaced = true
    return incoming
  })
  if (!replaced) {
    next.push(incoming)
  }
  return next
}

/**
 * 合并历史与 WS 事件的纯 reducer。
 *
 * Args:
 *   state: 当前时间线
 *   action: 历史加载 / 快照 / 用户发送 / WS envelope
 *
 * Returns:
 *   新状态
 */
export function reduceOpsChat(state: OpsChatState, action: OpsChatAction): OpsChatState {
  switch (action.type) {
    case "reset":
      return initialState

    case "history_loaded":
      return { items: mapHistoryToItems(action.messages) }

    case "snapshot_loaded": {
      const incoming = mapSnapshotToItems(action.snapshot)
      if (action.replace) {
        return { items: mergeReplaceSnapshot(state.items, incoming) }
      }
      return { items: mergePrependSnapshot(state.items, incoming) }
    }

    case "user_sent":
      return {
        items: [
          ...state.items,
          {
            kind: "user",
            id: action.clientId,
            content: action.content,
            pending: true,
          },
        ],
      }

    case "user_settled":
      return {
        items: state.items.map((item) =>
          item.kind === "user" && item.id === action.clientId
            ? { ...item, pending: false }
            : item,
        ),
      }

    case "ws":
      return applyWsMessage(state, action.message)

    default:
      return state
  }
}

/**
 * 将单条 WS 服务端消息合并进时间线。
 *
 * Args:
 *   state: 当前状态
 *   message: 已解析的 envelope
 *
 * Returns:
 *   新状态（monitor_alert 由 hook 侧单独处理，此处忽略）
 */
function applyWsMessage(
  state: OpsChatState,
  message: AgentWsServerMessage,
): OpsChatState {
  switch (message.type) {
    case "assistant_delta": {
      const text = readString(message.payload, "text")
      const done = Boolean(message.payload.done)
      const items = [...state.items]
      const last = items[items.length - 1]
      if (last?.kind === "assistant" && last.streaming) {
        items[items.length - 1] = {
          ...last,
          content: last.content + text,
          streaming: !done,
        }
      } else {
        items.push({
          kind: "assistant",
          id: nextEphemeralId("stream"),
          content: text,
          streaming: !done,
        })
      }
      return { items }
    }

    case "tool_call": {
      const toolCallId = readString(message.payload, "id")
      const name = readString(message.payload, "name") || "tool"
      return {
        items: [
          ...state.items,
          {
            kind: "tool_call",
            id: nextEphemeralId("toolcall"),
            toolCallId,
            name,
          },
        ],
      }
    }

    case "hitl_pending": {
      const proposalId = readProposalId(message.payload)
      if (proposalId == null) return state
      const id = hitlItemId(proposalId)
      if (state.items.some((item) => item.id === id)) return state
      return {
        items: [
          ...state.items,
          {
            kind: "hitl",
            id,
            proposalId,
            actionType: readString(message.payload, "action_type"),
            status: readString(message.payload, "status") || "pending",
            reason: readString(message.payload, "reason"),
            assetId: readAssetId(message.payload),
            resultExcerpt: readResultExcerpt(message.payload),
            hasFullResult: readHasFullResult(message.payload),
          },
        ],
      }
    }

    case "hitl_resolved":
    case "hitl_execution_failed": {
      const proposalId = readProposalId(message.payload)
      if (proposalId == null) return state
      const status =
        message.type === "hitl_execution_failed"
          ? readString(message.payload, "status") || "execution_failed"
          : readString(message.payload, "status") || "resolved"
      const items = state.items.map((item) => {
        if (item.kind !== "hitl" || item.proposalId !== proposalId) return item
        return {
          ...item,
          status,
          actionType:
            readString(message.payload, "action_type") || item.actionType,
          reason: readString(message.payload, "reason") || item.reason,
          assetId:
            message.payload.asset_id !== undefined
              ? readAssetId(message.payload)
              : item.assetId,
          resultExcerpt:
            message.payload.result_excerpt !== undefined
              ? readResultExcerpt(message.payload)
              : item.resultExcerpt,
          hasFullResult:
            message.payload.has_full_result !== undefined
              ? readHasFullResult(message.payload)
              : item.hasFullResult,
        }
      })
      const exists = items.some(
        (item) => item.kind === "hitl" && item.proposalId === proposalId,
      )
      if (!exists) {
        items.push({
          kind: "hitl",
          id: hitlItemId(proposalId),
          proposalId,
          actionType: readString(message.payload, "action_type"),
          status,
          reason: readString(message.payload, "reason"),
          assetId: readAssetId(message.payload),
          resultExcerpt: readResultExcerpt(message.payload),
          hasFullResult: readHasFullResult(message.payload),
        })
      }
      return { items }
    }

    case "error": {
      const errMessage =
        readString(message.payload, "message") || "发生错误，请稍后重试"
      return {
        items: [
          ...state.items,
          {
            kind: "error",
            id: nextEphemeralId("error"),
            message: errMessage,
          },
        ],
      }
    }

    case "turn_done": {
      return {
        items: state.items.map((item) =>
          item.kind === "assistant" && item.streaming
            ? { ...item, streaming: false }
            : item,
        ),
      }
    }

    case "monitor_alert":
      return state

    case "child_status": {
      const incoming = mapChildStatusPayload(message.payload)
      if (incoming == null) return state
      return { items: mergeChildStatusItem(state.items, incoming) }
    }

    default:
      return state
  }
}

export interface UseOpsChatOptions {
  sessionId: number | null | undefined
}

export interface UseOpsChatResult {
  messages: OpsChatItem[]
  isLoadingHistory: boolean
  isSending: boolean
  /** 发送中或无会话时禁用输入 */
  inputDisabled: boolean
  wsStatus: AgentWsStatus
  reconnecting: boolean
  monitorAlert: Record<string, unknown> | null
  clearMonitorAlert: () => void
  sendMessage: (content: string) => Promise<void>
  reloadSnapshot: () => Promise<void>
  loadOlder: () => Promise<void>
  hasMore: boolean
  isLoadingOlder: boolean
}

/**
 * 运维助手单会话聊天状态：快照、发送、WS 合并。
 *
 * Args:
 *   options.sessionId: 当前会话；切换时重新加载并重连 WS
 *
 * Returns:
 *   消息列表、发送/加载态、WS 连接态、告警横幅与操作函数
 */
export function useOpsChat({
  sessionId,
}: UseOpsChatOptions): UseOpsChatResult {
  const [state, dispatch] = useReducer(reduceOpsChat, initialState)
  const [isLoadingHistory, setIsLoadingHistory] = useState(
    () => sessionId != null,
  )
  const [isLoadingOlder, setIsLoadingOlder] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [nextBeforeMessageId, setNextBeforeMessageId] = useState<number | null>(
    null,
  )
  const [isSending, setIsSending] = useState(false)
  const [monitorAlert, setMonitorAlert] = useState<Record<string, unknown> | null>(
    null,
  )
  /** 快照已 settle 的 sessionId；与当前 sessionId 相等时才开 WS */
  const [snapshotReadySessionId, setSnapshotReadySessionId] = useState<
    number | null
  >(null)
  /** 本轮发送期间是否已收到 WS error（避免 HTTP catch 再插一行） */
  const wsErrorForTurnRef = useRef(false)
  const requestGenerationRef = useRef(0)
  const snapshotAbortRef = useRef<AbortController | null>(null)
  const catchUpGenerationRef = useRef(0)
  const catchUpAbortRef = useRef<AbortController | null>(null)
  const bufferedWsRef = useRef<{
    sessionId: number
    generation: number
    messages: AgentWsServerMessage[]
  } | null>(null)
  const activeSessionIdRef = useRef(sessionId)

  useEffect(() => {
    activeSessionIdRef.current = sessionId
    requestGenerationRef.current += 1
    snapshotAbortRef.current?.abort()
    catchUpGenerationRef.current += 1
    catchUpAbortRef.current?.abort()
    bufferedWsRef.current = null
    dispatch({ type: "reset" })
    setMonitorAlert(null)
    setSnapshotReadySessionId(null)
    setHasMore(false)
    setNextBeforeMessageId(null)
    setIsLoadingHistory(sessionId != null)
    setIsLoadingOlder(false)
  }, [sessionId])

  useEffect(() => {
    return () => {
      requestGenerationRef.current += 1
      catchUpGenerationRef.current += 1
      snapshotAbortRef.current?.abort()
      catchUpAbortRef.current?.abort()
      bufferedWsRef.current = null
    }
  }, [])

  const reloadSnapshot = useCallback(async (): Promise<void> => {
    if (sessionId !== activeSessionIdRef.current) return

    catchUpGenerationRef.current += 1
    catchUpAbortRef.current?.abort()
    bufferedWsRef.current = null

    if (sessionId == null) {
      dispatch({ type: "reset" })
      setMonitorAlert(null)
      setSnapshotReadySessionId(null)
      setHasMore(false)
      setNextBeforeMessageId(null)
      setIsLoadingHistory(false)
      return
    }

    setSnapshotReadySessionId(null)
    setIsLoadingHistory(true)
    const generation = ++requestGenerationRef.current
    const controller = new AbortController()
    snapshotAbortRef.current?.abort()
    snapshotAbortRef.current = controller

    try {
      const snapshot = await getAgentSessionSnapshot(
        sessionId,
        {},
        controller.signal,
      )
      if (
        controller.signal.aborted ||
        generation !== requestGenerationRef.current ||
        sessionId !== activeSessionIdRef.current
      ) {
        return
      }
      setHasMore(snapshot.has_more_messages)
      setNextBeforeMessageId(snapshot.next_before_message_id)
      dispatch({ type: "snapshot_loaded", snapshot, replace: true })
      setSnapshotReadySessionId(sessionId)
    } catch {
      if (
        !controller.signal.aborted &&
        generation === requestGenerationRef.current &&
        sessionId === activeSessionIdRef.current
      ) {
        toast.error("加载会话快照失败")
        setSnapshotReadySessionId(sessionId)
      }
    } finally {
      if (generation === requestGenerationRef.current) {
        setIsLoadingHistory(false)
      }
    }
  }, [sessionId])

  const loadOlder = useCallback(async (): Promise<void> => {
    if (
      sessionId == null ||
      sessionId !== activeSessionIdRef.current ||
      !hasMore ||
      isLoadingOlder ||
      nextBeforeMessageId == null
    ) {
      return
    }

    setIsLoadingOlder(true)
    const generation = ++requestGenerationRef.current
    const controller = new AbortController()
    snapshotAbortRef.current?.abort()
    snapshotAbortRef.current = controller

    try {
      const snapshot = await getAgentSessionSnapshot(
        sessionId,
        { before_message_id: nextBeforeMessageId },
        controller.signal,
      )
      if (
        controller.signal.aborted ||
        generation !== requestGenerationRef.current ||
        sessionId !== activeSessionIdRef.current
      ) {
        return
      }
      setHasMore(snapshot.has_more_messages)
      setNextBeforeMessageId(snapshot.next_before_message_id)
      dispatch({ type: "snapshot_loaded", snapshot, replace: false })
    } catch {
      if (!controller.signal.aborted) {
        toast.error("加载更早消息失败")
      }
    } finally {
      setIsLoadingOlder(false)
    }
  }, [sessionId, hasMore, isLoadingOlder, nextBeforeMessageId])

  useEffect(() => {
    void reloadSnapshot()
  }, [reloadSnapshot])

  const applyWsMessage = useCallback((message: AgentWsServerMessage) => {
    if (message.type === "monitor_alert") {
      setMonitorAlert(message.payload)
      return
    }
    if (message.type === "error") {
      wsErrorForTurnRef.current = true
    }
    dispatch({ type: "ws", message })
  }, [])

  const handleWsMessage = useCallback(
    (message: AgentWsServerMessage) => {
      const buffered = bufferedWsRef.current
      if (
        buffered != null &&
        buffered.sessionId === activeSessionIdRef.current
      ) {
        buffered.messages.push(message)
        return
      }
      applyWsMessage(message)
    },
    [applyWsMessage],
  )

  const catchUpSnapshot = useCallback(
    async (targetSessionId: number): Promise<void> => {
      if (targetSessionId !== activeSessionIdRef.current) return

      const previousBuffer = bufferedWsRef.current
      const generation = ++catchUpGenerationRef.current
      const controller = new AbortController()
      catchUpAbortRef.current?.abort()
      catchUpAbortRef.current = controller
      const buffered = {
        sessionId: targetSessionId,
        generation,
        messages:
          previousBuffer?.sessionId === targetSessionId
            ? [...previousBuffer.messages]
            : [],
      }
      bufferedWsRef.current = buffered

      try {
        const snapshot = await getAgentSessionSnapshot(
          targetSessionId,
          {},
          controller.signal,
        )
        if (
          controller.signal.aborted ||
          generation !== catchUpGenerationRef.current ||
          targetSessionId !== activeSessionIdRef.current
        ) {
          return
        }
        setHasMore(snapshot.has_more_messages)
        setNextBeforeMessageId(snapshot.next_before_message_id)
        dispatch({ type: "snapshot_loaded", snapshot, replace: true })
      } catch {
        if (
          !controller.signal.aborted &&
          generation === catchUpGenerationRef.current &&
          targetSessionId === activeSessionIdRef.current
        ) {
          toast.error("追平会话快照失败")
        }
      } finally {
        if (bufferedWsRef.current === buffered) {
          bufferedWsRef.current = null
          if (
            generation === catchUpGenerationRef.current &&
            targetSessionId === activeSessionIdRef.current
          ) {
            for (const message of buffered.messages) {
              applyWsMessage(message)
            }
          }
        }
      }
    },
    [applyWsMessage],
  )

  const snapshotReady = shouldEnableAgentWs(sessionId, snapshotReadySessionId)

  const handleWsStatusChange = useCallback(
    (status: AgentWsStatus) => {
      if (status === "open" && sessionId != null) {
        void catchUpSnapshot(sessionId)
      }
    },
    [sessionId, catchUpSnapshot],
  )

  const { status: wsStatus, reconnecting } = useAgentWs({
    sessionId,
    enabled: snapshotReady,
    onMessage: handleWsMessage,
    onStatusChange: handleWsStatusChange,
  })

  const sendMessage = useCallback(
    async (content: string): Promise<void> => {
      const trimmed = content.trim()
      if (!trimmed || sessionId == null || isSending) return

      const clientId = nextEphemeralId("local-user")
      dispatch({ type: "user_sent", clientId, content: trimmed })
      wsErrorForTurnRef.current = false
      setIsSending(true)
      try {
        await postAgentMessage(sessionId, { content: trimmed })
      } catch {
        toast.error("发送失败，请稍后重试")
        if (shouldSynthesizeSendError(wsErrorForTurnRef.current)) {
          dispatch({
            type: "ws",
            message: {
              type: "error",
              payload: { message: "发送失败，请稍后重试" },
            },
          })
        }
      } finally {
        dispatch({ type: "user_settled", clientId })
        setIsSending(false)
        await reloadSnapshot()
      }
    },
    [sessionId, isSending, reloadSnapshot],
  )

  const clearMonitorAlert = useCallback(() => {
    setMonitorAlert(null)
  }, [])

  return {
    messages: state.items,
    isLoadingHistory,
    isSending,
    inputDisabled: isSending || sessionId == null,
    wsStatus,
    reconnecting,
    monitorAlert,
    clearMonitorAlert,
    sendMessage,
    reloadSnapshot,
    loadOlder,
    hasMore,
    isLoadingOlder,
  }
}
