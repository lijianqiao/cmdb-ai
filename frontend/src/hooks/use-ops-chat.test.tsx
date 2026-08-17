/** useOpsChat hook 单测：快照恢复、请求竞态与分页触发 */

// @vitest-environment jsdom

import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type {
  AgentSessionSnapshot,
  AgentWsServerMessage,
} from "@/types/agent"

import { useOpsChat } from "./use-ops-chat"

vi.mock("@/lib/agent-api", () => ({
  getAgentSessionSnapshot: vi.fn(),
  postAgentMessage: vi.fn(),
  cancelAgentTurn: vi.fn(),
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
  cancelAgentTurn,
  getAgentSessionSnapshot,
  postAgentMessage,
} from "@/lib/agent-api"
import { useAgentWs } from "@/hooks/use-agent-ws"

const mockGetSnapshot = vi.mocked(getAgentSessionSnapshot)
const mockPostMessage = vi.mocked(postAgentMessage)
const mockCancelTurn = vi.mocked(cancelAgentTurn)
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
  let wsOnMessage: ((message: AgentWsServerMessage) => void) | undefined
  let wsOnStatusChange:
    | ((status: typeof wsStatus) => void)
    | undefined

  beforeEach(() => {
    mockGetSnapshot.mockReset()
    mockPostMessage.mockReset()
    mockUseAgentWs.mockReset()
    wsStatus = "idle"
    mockUseAgentWs.mockImplementation(({ onMessage, onStatusChange }) => {
      wsOnMessage = onMessage
      wsOnStatusChange = onStatusChange
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

  it("clears the previous session when the new snapshot fails", async () => {
    mockGetSnapshot
      .mockResolvedValueOnce(snapshotFor(1, "old"))
      .mockRejectedValueOnce(new Error("snapshot unavailable"))

    const { result, rerender } = renderHook(
      ({ sessionId }) => useOpsChat({ sessionId }),
      { initialProps: { sessionId: 1 as number | null } },
    )
    await waitFor(() =>
      expect(result.current.messages).toContainEqual(
        expect.objectContaining({ content: "old" }),
      ),
    )

    rerender({ sessionId: 2 })

    await waitFor(() => expect(result.current.isLoadingHistory).toBe(false))
    expect(result.current.messages).toEqual([])
  })

  it("does not let an old POST completion invalidate the new session", async () => {
    const pendingPost = deferred<{
      reason: string
      final_answer: null
      control: null
    }>()
    mockGetSnapshot
      .mockResolvedValueOnce(snapshotFor(1, "old"))
      .mockResolvedValueOnce(snapshotFor(2, "new"))
      .mockResolvedValue(snapshotFor(1, "stale reload"))
    mockPostMessage.mockReturnValueOnce(pendingPost.promise)

    const { result, rerender } = renderHook(
      ({ sessionId }) => useOpsChat({ sessionId }),
      { initialProps: { sessionId: 1 as number | null } },
    )
    await waitFor(() =>
      expect(result.current.messages).toContainEqual(
        expect.objectContaining({ content: "old" }),
      ),
    )

    let sendPromise!: Promise<void>
    act(() => {
      sendPromise = result.current.sendMessage("from old session")
    })
    rerender({ sessionId: 2 })
    await waitFor(() =>
      expect(result.current.messages).toContainEqual(
        expect.objectContaining({ content: "new" }),
      ),
    )

    pendingPost.resolve({ reason: "final", final_answer: null, control: null })
    await act(async () => {
      await sendPromise
    })

    expect(mockGetSnapshot.mock.calls.map(([id]) => id)).toEqual([1, 2])
    expect(result.current.messages).toEqual([
      expect.objectContaining({ content: "new" }),
    ])
    expect(mockUseAgentWs).toHaveBeenLastCalledWith(
      expect.objectContaining({ sessionId: 2, enabled: true }),
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

  it("reconciles the optimistic user row with the persisted message", async () => {
    mockGetSnapshot
      .mockResolvedValueOnce(buildSnapshot())
      .mockResolvedValueOnce(
        buildSnapshot({
          messages: [
            {
              id: 31,
              session_id: 3,
              role: "user",
              content: "你好",
              tool_call_id: null,
              tool_calls: null,
              created_at: "2026-08-14T00:00:00Z",
            },
          ],
        }),
      )

    const { result } = renderHook(() => useOpsChat({ sessionId: 3 }))
    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(1))

    await act(async () => {
      await result.current.sendMessage("你好")
    })

    expect(
      result.current.messages.filter((item) => item.kind === "user"),
    ).toEqual([
      expect.objectContaining({ id: "message:31", content: "你好" }),
    ])
  })

  it("WS 从断开到已连接后会 reloadSnapshot", async () => {
    mockGetSnapshot.mockResolvedValue(buildSnapshot())

    const { rerender } = renderHook(() => useOpsChat({ sessionId: 4 }))

    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(1))

    wsStatus = "reconnecting"
    rerender()
    act(() => {
      wsOnStatusChange?.("open")
    })
    wsStatus = "open"
    rerender()

    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(2))
  })

  it("replays WS events received during the initial catch-up snapshot", async () => {
    const catchUp = deferred<AgentSessionSnapshot>()
    mockGetSnapshot
      .mockResolvedValueOnce(buildSnapshot())
      .mockReturnValueOnce(catchUp.promise)

    const { result, rerender } = renderHook(() =>
      useOpsChat({ sessionId: 9 }),
    )
    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(1))

    wsStatus = "connecting"
    rerender()
    act(() => {
      wsOnStatusChange?.("open")
      wsOnMessage?.({
        type: "child_status",
        payload: {
          child_id: "child-1",
          role: "ops_explorer",
          task_brief: "inspect asset",
          status: "RUNNING",
          result_summary: null,
        },
      })
    })
    wsStatus = "open"
    rerender()
    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(2))

    await act(async () => {
      catchUp.resolve(
        buildSnapshot({
          children: [
            {
              child_id: "child-1",
              role: "ops_explorer",
              task_brief: "inspect asset",
              status: "SPAWNING",
              result_summary: null,
              created_at: "2026-08-14T00:00:00Z",
              status_changed_at: "2026-08-14T00:00:00Z",
            },
          ],
        }),
      )
      await catchUp.promise
    })

    await waitFor(() =>
      expect(result.current.messages).toContainEqual(
        expect.objectContaining({ childId: "child-1", status: "RUNNING" }),
      ),
    )
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

  it("生成中调用 cancelTurn 会撤回本轮请求", async () => {
    mockGetSnapshot.mockResolvedValue(buildSnapshot())
    const pendingPost = deferred<{
      reason: string
      final_answer: null
      control: null
    }>()
    mockPostMessage.mockReturnValueOnce(pendingPost.promise)
    mockCancelTurn.mockResolvedValue(true)

    const { result } = renderHook(() => useOpsChat({ sessionId: 9 }))
    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(1))

    let sendPromise!: Promise<void>
    act(() => {
      sendPromise = result.current.sendMessage("查一下核心交换机")
    })
    await waitFor(() => expect(result.current.isSending).toBe(true))

    await act(async () => {
      await result.current.cancelTurn()
    })
    expect(mockCancelTurn).toHaveBeenCalledWith(9)

    // 收尾仍由那条 POST 自己完成：解除 isSending 并重新拉快照
    pendingPost.resolve({ reason: "cancelled", final_answer: null, control: null })
    await act(async () => {
      await sendPromise
    })
    expect(result.current.isSending).toBe(false)
    expect(mockGetSnapshot).toHaveBeenCalledTimes(2)
  })

  it("没有正在生成的回答时 cancelTurn 不发请求", async () => {
    mockGetSnapshot.mockResolvedValue(buildSnapshot())

    const { result } = renderHook(() => useOpsChat({ sessionId: 10 }))
    await waitFor(() => expect(mockGetSnapshot).toHaveBeenCalledTimes(1))

    await act(async () => {
      await result.current.cancelTurn()
    })

    expect(mockCancelTurn).not.toHaveBeenCalled()
  })
})
