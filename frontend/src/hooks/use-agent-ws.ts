/** Agent WebSocket 生命周期 hook

 * 按 session 连接 `/api/v1/ws/agent/{id}`，token 与 axios 同源（`getAccessToken`）。
 * 断线后按 `nextReconnectDelay` 指数退避重连（架构 A5）；不自造 ping。
 */

import { useEffect, useRef, useState } from "react"

import { getAccessToken } from "@/lib/api"
import {
  buildAgentWsUrl,
  nextReconnectDelay,
  parseAgentWsMessage,
} from "@/lib/agent-ws"
import { useAuthStore } from "@/store/auth"
import type { AgentWsServerMessage } from "@/types/agent"

/** WS 连接状态（供 Task 7 UI 展示「重连中」等） */
export type AgentWsStatus =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed"

export interface UseAgentWsOptions {
  /** 目标会话；为空则不连接 */
  sessionId: number | null | undefined
  /** 是否启用连接，默认 true */
  enabled?: boolean
  /** 解析成功后的服务端消息回调 */
  onMessage?: (message: AgentWsServerMessage) => void
}

export interface UseAgentWsResult {
  status: AgentWsStatus
  /** 是否处于断线重连等待/再连中 */
  reconnecting: boolean
}

/**
 * 管理单会话 Agent WebSocket：连接、收消息、指数退避重连。
 *
 * Args:
 *   options.sessionId: 会话 ID
 *   options.enabled: 为 false 时主动断开并不再重连
 *   options.onMessage: 合法 envelope 回调（经 ref，避免重连抖动）
 *
 * Returns:
 *   status / reconnecting，供页面展示连接态
 */
export function useAgentWs({
  sessionId,
  enabled = true,
  onMessage,
}: UseAgentWsOptions): UseAgentWsResult {
  const [status, setStatus] = useState<AgentWsStatus>("idle")
  const token = useAuthStore((state) => state.token)
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage

  useEffect(() => {
    if (!enabled || sessionId == null) {
      setStatus("idle")
      return
    }

    let disposed = false
    let socket: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let attempt = 0
    let intentionalClose = false

    const clearReconnectTimer = (): void => {
      if (reconnectTimer != null) {
        clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
    }

    const connect = (): void => {
      if (disposed) return

      // 与 axios 拦截器同源，不复制 token 状态
      const accessToken = getAccessToken() ?? token
      if (!accessToken) {
        setStatus("closed")
        return
      }

      setStatus(attempt > 0 ? "reconnecting" : "connecting")
      const url = buildAgentWsUrl(sessionId, accessToken)
      socket = new WebSocket(url)

      socket.onopen = () => {
        if (disposed) return
        attempt = 0
        setStatus("open")
      }

      socket.onmessage = (event: MessageEvent) => {
        if (disposed) return
        const raw =
          typeof event.data === "string" ? event.data : String(event.data)
        const message = parseAgentWsMessage(raw)
        if (message) {
          onMessageRef.current?.(message)
        }
      }

      socket.onerror = () => {
        // 浏览器随后会触发 onclose；重连逻辑集中在 onclose
      }

      socket.onclose = () => {
        socket = null
        if (disposed || intentionalClose) {
          if (!disposed) {
            setStatus("closed")
          }
          return
        }
        setStatus("reconnecting")
        const delay = nextReconnectDelay(attempt)
        attempt += 1
        clearReconnectTimer()
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null
          connect()
        }, delay)
      }
    }

    connect()

    return () => {
      disposed = true
      intentionalClose = true
      clearReconnectTimer()
      if (socket) {
        socket.onopen = null
        socket.onmessage = null
        socket.onerror = null
        socket.onclose = null
        socket.close()
        socket = null
      }
    }
  }, [sessionId, enabled, token])

  return {
    status,
    reconnecting: status === "reconnecting",
  }
}
