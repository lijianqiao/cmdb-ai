/** 运维助手会话状态 hook

 * 加载 REST 历史 →（历史 settle 后）开 WS → 本地消息列表 → POST 发送 → 合并实时事件。
 * 消息合并抽为纯 reducer；isSending 仅由 POST finally 解除（turn_done 不解锁）。
 */

import { useCallback, useEffect, useReducer, useRef, useState } from "react"
import { toast } from "sonner"

import { listAgentMessages, postAgentMessage } from "@/lib/agent-api"
import { useAgentWs, type AgentWsStatus } from "@/hooks/use-agent-ws"
import type { AgentMessage, AgentWsServerMessage } from "@/types/agent"

/**
 * 是否允许连接 Agent WS。
 * 仅在「当前 session 的历史已 settle」后为 true，避免 GET 与实时事件竞态。
 */
export function shouldEnableAgentWs(
  sessionId: number | null | undefined,
  historyReadySessionId: number | null,
): boolean {
  return sessionId != null && historyReadySessionId === sessionId
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
    }
  | {
      kind: "error"
      id: string
      message: string
    }

export interface OpsChatState {
  items: OpsChatItem[]
}

export type OpsChatAction =
  | { type: "reset" }
  | { type: "history_loaded"; messages: AgentMessage[] }
  | { type: "user_sent"; clientId: string; content: string }
  | { type: "ws"; message: AgentWsServerMessage }

const initialState: OpsChatState = { items: [] }

let ephemeralSeq = 0

/** 生成仅前端可见的临时 id */
function nextEphemeralId(prefix: string): string {
  ephemeralSeq += 1
  return `${prefix}-${ephemeralSeq}`
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
        id: `msg-${row.id}`,
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
            id: `toolcall-${row.id}-${toolCallId || nextEphemeralId("tc")}`,
            toolCallId,
            name,
          })
        }
      }
      if (row.content) {
        items.push({
          kind: "assistant",
          id: `msg-${row.id}`,
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

/**
 * 合并历史与 WS 事件的纯 reducer。
 *
 * Args:
 *   state: 当前时间线
 *   action: 历史加载 / 用户发送 / WS envelope
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

    case "user_sent":
      return {
        items: [
          ...state.items,
          {
            kind: "user",
            id: action.clientId,
            content: action.content,
          },
        ],
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
      return {
        items: [
          ...state.items,
          {
            kind: "hitl",
            id: `hitl-${proposalId}`,
            proposalId,
            actionType: readString(message.payload, "action_type"),
            status: readString(message.payload, "status") || "pending",
            reason: readString(message.payload, "reason"),
            assetId: readAssetId(message.payload),
            resultExcerpt: readResultExcerpt(message.payload),
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
      const excerptFromPayload = readResultExcerpt(message.payload)
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
          resultExcerpt: excerptFromPayload ?? item.resultExcerpt,
        }
      })
      const exists = items.some(
        (item) => item.kind === "hitl" && item.proposalId === proposalId,
      )
      if (!exists) {
        items.push({
          kind: "hitl",
          id: `hitl-${proposalId}`,
          proposalId,
          actionType: readString(message.payload, "action_type"),
          status,
          reason: readString(message.payload, "reason"),
          assetId: readAssetId(message.payload),
          resultExcerpt: excerptFromPayload,
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
  reloadHistory: () => Promise<void>
}

/**
 * 运维助手单会话聊天状态：历史、发送、WS 合并。
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
  const [isSending, setIsSending] = useState(false)
  const [monitorAlert, setMonitorAlert] = useState<Record<string, unknown> | null>(
    null,
  )
  /** 历史已 settle 的 sessionId；与当前 sessionId 相等时才开 WS */
  const [historyReadySessionId, setHistoryReadySessionId] = useState<number | null>(
    null,
  )
  /** 本轮发送期间是否已收到 WS error（避免 HTTP catch 再插一行） */
  const wsErrorForTurnRef = useRef(false)

  const reloadHistory = useCallback(async (): Promise<void> => {
    if (sessionId == null) {
      dispatch({ type: "reset" })
      setMonitorAlert(null)
      setHistoryReadySessionId(null)
      setIsLoadingHistory(false)
      return
    }
    // 加载期间关闭 WS，避免 history_loaded 整表替换冲掉已到的实时事件
    setHistoryReadySessionId(null)
    setIsLoadingHistory(true)
    try {
      const rows = await listAgentMessages(sessionId)
      dispatch({ type: "history_loaded", messages: rows })
      setHistoryReadySessionId(sessionId)
    } catch {
      toast.error("加载会话历史失败")
      // 失败也放行 WS，便于用户继续发消息；时间线可能仍为空
      setHistoryReadySessionId(sessionId)
    } finally {
      setIsLoadingHistory(false)
    }
  }, [sessionId])

  useEffect(() => {
    void reloadHistory()
  }, [reloadHistory])

  const handleWsMessage = useCallback((message: AgentWsServerMessage) => {
    if (message.type === "monitor_alert") {
      setMonitorAlert(message.payload)
      return
    }
    if (message.type === "error") {
      wsErrorForTurnRef.current = true
    }
    // turn_done 只收尾时间线（streaming→false）；isSending 仅由 POST finally 解除
    dispatch({ type: "ws", message })
  }, [])

  const historyReady = shouldEnableAgentWs(sessionId, historyReadySessionId)

  const { status: wsStatus, reconnecting } = useAgentWs({
    sessionId,
    enabled: historyReady,
    onMessage: handleWsMessage,
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
        setIsSending(false)
      }
    },
    [sessionId, isSending],
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
    reloadHistory,
  }
}
