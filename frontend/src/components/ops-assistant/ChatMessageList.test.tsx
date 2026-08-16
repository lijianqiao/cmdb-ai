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

  it("最终回答生成完成后折叠中间过程，并提供提问与回答的复制按钮", () => {
    render(
      <ChatMessageList
        sessionId={10}
        messages={[
          { kind: "user", id: "msg:1", content: "查一下设备状态" },
          { kind: "tool_call", id: "tc:1", toolCallId: "c1", name: "ping_target" },
          { kind: "assistant", id: "msg:2", content: "设备在线，延迟 2ms", streaming: false },
        ]}
      />,
    )

    // 用户提问和模型回答渲染
    expect(screen.getByText("查一下设备状态")).toBeInTheDocument()
    expect(screen.getByText("设备在线，延迟 2ms")).toBeInTheDocument()

    // 复制按钮存在（提问与回答各1个）
    expect(
      screen.getByRole("button", { name: "复制提问内容" }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole("button", { name: "复制回答内容" }),
    ).toBeInTheDocument()

    // 点击展开
    fireEvent.click(screen.getByRole("button", { name: /思考与执行过程/ }))
    expect(screen.getByText("工具调用 · ping_target")).toBeInTheDocument()
  })

  it("长提问（>5行）默认显示展开提示，点击气泡切换展开与收起", () => {
    const longQuestion = "第一行\n第二行\n第三行\n第四行\n第五行\n第六行详细说明"
    render(
      <ChatMessageList
        sessionId={10}
        messages={[
          { kind: "user", id: "msg:1", content: longQuestion },
          { kind: "assistant", id: "msg:2", content: "已收到", streaming: false },
        ]}
      />,
    )

    // 默认展示“点击展开全文”
    expect(screen.getByText("点击展开全文")).toBeInTheDocument()

    // 点击气泡展开
    const bubble = screen.getByRole("button", { name: /第一行/ })
    fireEvent.click(bubble)
    expect(screen.getByText("点击收起")).toBeInTheDocument()

    // 再次点击收起
    fireEvent.click(bubble)
    expect(screen.getByText("点击展开全文")).toBeInTheDocument()
  })

  it("多步骤问答中，中间的 assistant 阶段性说明文字全部折入执行过程折叠面板", () => {
    render(
      <ChatMessageList
        sessionId={10}
        messages={[
          { kind: "user", id: "msg:1", content: "帮我查询资产并备份" },
          { kind: "assistant", id: "a:1", content: "第一步：正在获取资产详情...", streaming: false },
          { kind: "tool_call", id: "tc:1", toolCallId: "c1", name: "list_assets" },
          { kind: "assistant", id: "a:2", content: "资产查询完毕，最终备份成功！", streaming: false },
        ]}
      />,
    )

    // 最终回答必须展示在最外层
    expect(screen.getByText("资产查询完毕，最终备份成功！")).toBeInTheDocument()

    // 中间阶段性说明文字默认被折叠，最外层不可见
    expect(screen.queryByText("第一步：正在获取资产详情...")).not.toBeInTheDocument()

    // 点击展开后可见中间说明文字与工具调用
    fireEvent.click(screen.getByRole("button", { name: /思考与执行过程/ }))
    expect(screen.getByText("第一步：正在获取资产详情...")).toBeInTheDocument()
    expect(screen.getByText("工具调用 · list_assets")).toBeInTheDocument()
  })
})
