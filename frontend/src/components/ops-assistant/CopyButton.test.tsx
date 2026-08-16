/** CopyButton 组件单测：剪贴板复制、状态切换与 Toast 提示 */

// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { toast } from "sonner"

import { CopyButton } from "./CopyButton"

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

describe("CopyButton", () => {
  let writeTextMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    writeTextMock = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, {
      clipboard: {
        writeText: writeTextMock,
      },
    })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("点击后调用剪贴板 API 并触发成功提示", async () => {
    const textToCopy = "# 标题内容\n一些 Markdown"
    render(<CopyButton text={textToCopy} />)

    const btn = screen.getByRole("button", { name: "复制 Markdown 内容" })
    fireEvent.click(btn)

    await waitFor(() => {
      expect(writeTextMock).toHaveBeenCalledWith(textToCopy)
      expect(toast.success).toHaveBeenCalledWith("已复制 Markdown 内容")
    })

    expect(screen.getByRole("button", { name: "已复制" })).toBeInTheDocument()
  })

  it("文本为空时不触发复制", async () => {
    render(<CopyButton text="" />)

    const btn = screen.getByRole("button", { name: "复制 Markdown 内容" })
    fireEvent.click(btn)

    expect(writeTextMock).not.toHaveBeenCalled()
  })
})
