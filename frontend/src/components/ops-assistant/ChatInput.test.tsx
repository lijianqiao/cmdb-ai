/** ChatInput 单测：审批档位选择器挂载 */

// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ChatInput } from "./ChatInput"

afterEach(() => {
  cleanup()
})

describe("ChatInput", () => {
  it("展示当前审批档位选择器", () => {
    const onApprovalModeSelect = vi.fn()
    render(
      <ChatInput
        approvalMode="assist"
        onApprovalModeSelect={onApprovalModeSelect}
        onSend={vi.fn()}
      />,
    )

    expect(screen.getByLabelText("审批模式")).toBeInTheDocument()
    expect(screen.getByText("帮我审批")).toBeInTheDocument()
  })

  it("无会话时禁用审批档位选择器", () => {
    render(
      <ChatInput
        approvalMode={null}
        onApprovalModeSelect={vi.fn()}
        onSend={vi.fn()}
      />,
    )

    expect(screen.getByLabelText("审批模式")).toHaveAttribute("disabled")
  })

  it("isSending 为 true 时禁用输入框并展示'生成中...'按钮", () => {
    render(
      <ChatInput
        approvalMode="ask"
        isSending={true}
        onApprovalModeSelect={vi.fn()}
        onSend={vi.fn()}
      />,
    )

    expect(screen.getByLabelText("消息输入")).toBeDisabled()
    expect(screen.getByRole("button", { name: "生成中" })).toBeDisabled()
    expect(screen.getByText("生成中...")).toBeInTheDocument()
  })

  it("生成中悬停按钮变成停止，点击触发 onCancel", () => {
    const onCancel = vi.fn()
    const onSend = vi.fn()
    render(
      <ChatInput
        approvalMode="ask"
        isSending={true}
        onApprovalModeSelect={vi.fn()}
        onSend={onSend}
        onCancel={onCancel}
      />,
    )

    const button = screen.getByRole("button", { name: "生成中" })
    expect(button).toBeEnabled()

    fireEvent.mouseEnter(button)
    expect(screen.getByRole("button", { name: "停止生成" })).toBeInTheDocument()
    expect(screen.getByText("停止")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "停止生成" }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onSend).not.toHaveBeenCalled()
  })

  it("没传 onCancel 时生成中不出现停止入口", () => {
    render(
      <ChatInput
        approvalMode="ask"
        isSending={true}
        onApprovalModeSelect={vi.fn()}
        onSend={vi.fn()}
      />,
    )

    fireEvent.mouseEnter(screen.getByRole("button", { name: "生成中" }))
    expect(screen.queryByText("停止")).not.toBeInTheDocument()
  })

  it("本轮结束后悬停态复位，不会一上来就显示停止", () => {
    const { rerender } = render(
      <ChatInput
        approvalMode="ask"
        isSending={true}
        onApprovalModeSelect={vi.fn()}
        onSend={vi.fn()}
        onCancel={vi.fn()}
      />,
    )

    // 鼠标停在按钮上不动，本轮结束 → 再开一轮：mouseleave 不会触发，
    // 悬停态必须由 isSending 变化来复位，否则下一轮直接显示成停止按钮。
    fireEvent.mouseEnter(screen.getByRole("button", { name: "生成中" }))
    rerender(
      <ChatInput
        approvalMode="ask"
        isSending={false}
        onApprovalModeSelect={vi.fn()}
        onSend={vi.fn()}
        onCancel={vi.fn()}
      />,
    )
    rerender(
      <ChatInput
        approvalMode="ask"
        isSending={true}
        onApprovalModeSelect={vi.fn()}
        onSend={vi.fn()}
        onCancel={vi.fn()}
      />,
    )

    expect(screen.getByText("生成中...")).toBeInTheDocument()
    expect(screen.queryByText("停止")).not.toBeInTheDocument()
  })
})
