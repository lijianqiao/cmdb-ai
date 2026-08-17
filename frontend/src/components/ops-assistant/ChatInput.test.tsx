/** ChatInput 单测：审批档位选择器挂载 */

// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
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
})
