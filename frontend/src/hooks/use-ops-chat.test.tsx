/** useOpsChat hook 单测：快照恢复、请求竞态与分页触发 */

// @vitest-environment jsdom

import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { AgentSessionSnapshot } from "@/types/agent"

import { useOpsChat } from "./use-ops-chat"

vi.mock("@/lib/agent-api", () => ({
  getAgentSessionSnapshot: vi.fn(),
  postAgentMessage: vi.fn(),
}))

vi.mock("@/hooks/use-agent-ws", () => ({
  useAgentWs: vi.fn(),
}))

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}))

import {
  getAgentSessionSnapshot,
  postAgentMessage,
} from "@/lib/agent-api"
import { useAgentWs } from "@/hooks/use-agent-ws"

const mockGetSnapshot = vi.mocked(getAgentSessionSnapshot)
const mockPostMessage = vi.mocked(postAgentMessage)
const mockUseAgentWs = vi.mocked(useAgentWs)

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

function buildSnapshot(
  overrides: Partial<AgentSessionSnapshot> = {},
): AgentSessionSnapshot {
  return Object.assign(
    {
      messages: [],
      proposals: [],
      children: [],
      has_more_messages: false,
      next_before_message_id: null,
    },
    overrides,
  )
}

function snapshotFor(sessionId: number, content: string): AgentSessionSnapshot {
  return buildSnapshot({
    messages: [
      {
        id: sessionId,
        session_id: sessionId,
        role: "assistant",
        content,
        tool_call_id: null,
        tool_calls: null,
        created_at: "2026-08-14T00:00:00Z",
      },
    ],
  })
}

describe("useOpsChat snapshot recovery", () => {
  let wsStatus: "idle" | "connecting" | "open" | "reconnecting" | "closed" =
    "idle"

  beforeEach(() => {
    wsStatus = "idle"
    mockUseAgentWs.mockImplementation(({ onMessage }) => {
      void onMessage
      return { status: wsStatus, reconnecting: wsStatus === "reconnecting" }
    })
    mockPostMessage.mockResolvedValue({
      reason: "final",
      final_answer: null,
      control: null,
    })
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it("ignores a snapshot response from the previous session", async () => {
    const first = deferred<AgentSessionSnapshot>()
    const second = deferred<AgentSessionSnapshot>()
    mockGetSnapshot
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)

    const { result, rerender } = renderHook(
      ({ sessionId }) => useOpsChat({ sessionId }),
      { initialProps: { sessionId: 1 as number | null } },
    )
    rerender({ sessionId: 2 })
    second.resolve(snapshotFor(2, "new"))
    first.resolve(snapshotFor(1, "old"))

    await waitFor(() =>
      expect(result.current.messages).toContainEqual(
        expect.objectContaining({ content: "new" }),
      ),
    )
    expect(result.current.messages).not.toContainEqual(
      expect.objectContaining({ content: "old" }),
    )
  })

  it("POST 完成后会 reloadSnapshot", async () => {
    mockGetSnapshot.mockResolvedValue(buildSnapshot())

    const { result } = renderHook(() => useOpsChat({ sessionId: 3 }))

    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(1))

    await act(async () => {
      await result.current.sendMessage("你好")
    })

    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(2))
  })

  it("WS 从断开到已连接后会 reloadSnapshot", async () => {
    mockGetSnapshot.mockResolvedValue(buildSnapshot())

    const { rerender } = renderHook(() => useOpsChat({ sessionId: 4 }))

    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(1))

    wsStatus = "reconnecting"
    rerender()
    wsStatus = "open"
    rerender()

    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(2))
  })

  it("unmount 会 abort 进行中的 snapshot 请求", async () => {
    const pending = deferred<AgentSessionSnapshot>()
    mockGetSnapshot.mockReturnValueOnce(pending.promise)

    const abortSpy = vi.spyOn(AbortController.prototype, "abort")
    const { unmount } = renderHook(() => useOpsChat({ sessionId: 5 }))

    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(1))
    unmount()

    expect(abortSpy).toHaveBeenCalled()
    abortSpy.mockRestore()
  })

  it("POST 触发的 reloadSnapshot 不会让 isLoadingOlder 卡住", async () => {
    mockGetSnapshot
      .mockResolvedValueOnce(
        buildSnapshot({
          has_more_messages: true,
          next_before_message_id: 5,
        }),
      )
      .mockImplementation((_sessionId, params, signal) => {
        if (params?.before_message_id != null) {
          return new Promise<AgentSessionSnapshot>((_resolve, reject) => {
            signal?.addEventListener("abort", () => {
              reject(new DOMException("Aborted", "AbortError"))
            })
          })
        }
        return Promise.resolve(buildSnapshot())
      })

    const { result } = renderHook(() => useOpsChat({ sessionId: 6 }))

    await waitFor(() => expect(result.current.hasMore).toBe(true))

    await act(async () => {
      void result.current.loadOlder()
    })
    expect(result.current.isLoadingOlder).toBe(true)

    await act(async () => {
      await result.current.sendMessage("你好")
    })

    await waitFor(() => expect(result.current.isLoadingOlder).toBe(false))
  })

  it("切换会话会重置 isLoadingOlder", async () => {
    const olderPending = deferred<AgentSessionSnapshot>()
    mockGetSnapshot
      .mockResolvedValueOnce(
        buildSnapshot({
          has_more_messages: true,
          next_before_message_id: 5,
        }),
      )
      .mockReturnValueOnce(olderPending.promise)
      .mockResolvedValue(buildSnapshot())

    const { result, rerender } = renderHook(
      ({ sessionId }) => useOpsChat({ sessionId }),
      { initialProps: { sessionId: 7 as number | null } },
    )

    await waitFor(() => expect(result.current.hasMore).toBe(true))

    await act(async () => {
      void result.current.loadOlder()
    })
    expect(result.current.isLoadingOlder).toBe(true)

    rerender({ sessionId: 8 })

    await waitFor(() => expect(result.current.isLoadingOlder).toBe(false))
  })
})
