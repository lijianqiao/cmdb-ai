/** ChatMessageList 单测：滚动保持与分页 sentinel */

// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { OpsChatItem } from "@/hooks/use-ops-chat"

import { ChatMessageList } from "./ChatMessageList"

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
      <ChatMessageList messages={initialMessages} hasMore onLoadOlder={vi.fn()} />,
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
})
