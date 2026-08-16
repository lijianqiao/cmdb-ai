/** ChatMessageList 单测：滚动保持与分页 sentinel */

// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { OpsChatItem } from "@/hooks/use-ops-chat"

import { ChatMessageList } from "./ChatMessageList"

vi.mock("@/hooks/use-permission", () => ({
  usePermission: () => ({
    permissions: [],
    hasPermission: () => false,
    hasAnyPermission: () => false,
    hasAllPermissions: () => false,
  }),
}))

vi.mock("@/lib/agent-api", () => ({
  getDeviceQueryResult: vi.fn(),
  recoverDeviceQuerySummary: vi.fn(),
}))

import { getDeviceQueryResult } from "@/lib/agent-api"

const mockGetDeviceQueryResult = vi.mocked(getDeviceQueryResult)

function userMessage(id: string, content: string): OpsChatItem {
  return { kind: "user", id, content }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("ChatMessageList scroll and pagination", () => {
  let observerOptions: IntersectionObserverInit | undefined
  let scrollHeightForTest = 400

  beforeEach(() => {
    observerOptions = undefined
    scrollHeightForTest = 400
    Element.prototype.scrollIntoView = vi.fn()
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor(
          _callback: IntersectionObserverCallback,
          options?: IntersectionObserverInit,
        ) {
          observerOptions = options
        }

        observe = vi.fn()
        disconnect = vi.fn()
      },
    )
    vi.stubGlobal(
      "requestAnimationFrame",
      (callback: FrameRequestCallback) => {
        scrollHeightForTest = 520
        callback(0)
        return 0
      },
    )
  })

  it("IntersectionObserver 使用滚动容器作为 root", () => {
    render(
      <ChatMessageList
        sessionId={10}
        messages={[userMessage("message:1", "你好")]}
        hasMore
        onLoadOlder={vi.fn()}
      />,
    )

    const scrollContainer = document.querySelector(
      "[data-slot='scroll-area']",
    )
    expect(scrollContainer).not.toBeNull()
    expect(observerOptions?.root).toBe(scrollContainer)
  })

  it("前插消息时保持滚动位置", () => {
    const initialMessages = [
      userMessage("message:2", "第二条"),
      userMessage("message:3", "第三条"),
    ]
    const { rerender } = render(
      <ChatMessageList sessionId={10} messages={initialMessages} hasMore onLoadOlder={vi.fn()} />,
    )

    const scrollContainer = document.querySelector(
      "[data-slot='scroll-area']",
    ) as HTMLDivElement
    Object.defineProperty(scrollContainer, "scrollHeight", {
      configurable: true,
      get: () => scrollHeightForTest,
    })
    scrollContainer.scrollTop = 120

    rerender(
      <ChatMessageList
        sessionId={10}
        messages={[
          userMessage("message:1", "第一条"),
          ...initialMessages,
        ]}
        hasMore
        onLoadOlder={vi.fn()}
      />,
    )

    expect(scrollContainer.scrollTop).toBe(240)
  })

  it("把当前 sessionId 传给真实 HITL 卡片", async () => {
    mockGetDeviceQueryResult.mockResolvedValue({
      proposal_id: 7,
      content: "full config",
      content_length: 11,
      summary_status: "completed",
      created_at: "2026-08-15T10:00:00Z",
    })
    render(
      <ChatMessageList
        sessionId={22}
        messages={[
          {
            kind: "hitl",
            id: "hitl:7",
            proposalId: 7,
            actionType: "device_query",
            status: "EXECUTED",
            reason: "排查交换机",
            assetId: 9,
            resultExcerpt: "preview",
            hasFullResult: true,
          },
        ]}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))
    await waitFor(() => {
      expect(mockGetDeviceQueryResult).toHaveBeenCalledWith(22, 7)
    })
  })
})
