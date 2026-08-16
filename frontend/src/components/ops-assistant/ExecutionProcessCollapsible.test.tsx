/** ExecutionProcessCollapsible 组件单测：展开折叠、状态汇总文案与手动切换 */

// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { OpsChatItem } from "@/hooks/use-ops-chat"
import { ExecutionProcessCollapsible } from "./ExecutionProcessCollapsible"

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

const mockItems: OpsChatItem[] = [
  {
    kind: "tool_call",
    id: "tc:1",
    toolCallId: "call_1",
    name: "ping_target",
  },
  {
    kind: "child",
    id: "child:1",
    childId: "c_1",
    role: "network_probe",
    taskBrief: "检测网关延迟",
    status: "COMPLETED",
    resultSummary: "延迟正常",
  },
]

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe("ExecutionProcessCollapsible", () => {
  it("生成中（isGenerating=true）默认展开并显示正在执行文案", () => {
    render(
      <ExecutionProcessCollapsible
        sessionId={1}
        items={mockItems}
        isGenerating={true}
      />,
    )

    expect(
      screen.getByText("正在执行 (1 个工具 · 1 个子任务)"),
    ).toBeInTheDocument()
    expect(screen.getByText("工具调用 · ping_target")).toBeInTheDocument()
    expect(screen.getByText("检测网关延迟")).toBeInTheDocument()
  })

  it("已完成（isGenerating=false）默认折叠，不展示内部条目", () => {
    render(
      <ExecutionProcessCollapsible
        sessionId={1}
        items={mockItems}
        isGenerating={false}
      />,
    )

    expect(
      screen.getByText("思考与执行过程 (1 个工具 · 1 个子任务)"),
    ).toBeInTheDocument()
    expect(screen.queryByText("工具调用 · ping_target")).not.toBeInTheDocument()
  })

  it("点击头部折叠条可以手动展开与收起", () => {
    render(
      <ExecutionProcessCollapsible
        sessionId={1}
        items={mockItems}
        isGenerating={false}
      />,
    )

    const toggleBtn = screen.getByRole("button", {
      name: /思考与执行过程/,
    })

    // 点击展开
    fireEvent.click(toggleBtn)
    expect(screen.getByText("工具调用 · ping_target")).toBeInTheDocument()

    // 再次点击收起
    fireEvent.click(toggleBtn)
    expect(screen.queryByText("工具调用 · ping_target")).not.toBeInTheDocument()
  })
})
