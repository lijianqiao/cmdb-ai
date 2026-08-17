/** DocumentPreviewDrawer 单测：正文渲染、截断提示与 md/txt 的区别对待 */

// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, describe, expect, it, vi } from "vitest"

import { getDocumentContent, type KnowledgeDocument } from "@/lib/knowledge-api"

import { DocumentPreviewDrawer } from "./DocumentPreviewDrawer"

vi.mock("@/lib/knowledge-api", () => ({
  getDocumentContent: vi.fn(),
}))

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function buildDocument(fileType: string): KnowledgeDocument {
  return {
    id: 7,
    category_id: 1,
    title: "交换机手册",
    original_filename: `m.${fileType}`,
    file_path: `sop/7_m.${fileType}`,
    file_type: fileType,
    status: "ready",
    created_at: "2026-08-18T00:00:00Z",
    suggested_category_id: null,
    suggestion_confidence: null,
    suggestion_reason: "",
    suggested_at: null,
  } as KnowledgeDocument
}

describe("DocumentPreviewDrawer", () => {
  it("按文档 ID 拉取正文并以 Markdown 渲染", async () => {
    vi.mocked(getDocumentContent).mockResolvedValue({
      document_id: 7,
      title: "交换机手册",
      file_type: "md",
      content: "# 一级标题\n\n正文段落",
      total_chars: 12,
      offset: 0,
      truncated: false,
    })

    render(
      <DocumentPreviewDrawer document={buildDocument("md")} onClose={vi.fn()} />,
    )

    expect(await screen.findByText("正文段落")).toBeInTheDocument()
    // md 走 Markdown 渲染：# 应该变成标题元素，而不是原样的井号
    expect(await screen.findByRole("heading", { name: "一级标题" })).toBeInTheDocument()
    expect(getDocumentContent).toHaveBeenCalledWith(7)
  })

  it("txt 保持原样输出，不当成 Markdown 解析", async () => {
    // 纯文本喂给 Markdown 渲染器会把行首的 # 吞成标题，看到的就不是文件真正的样子
    vi.mocked(getDocumentContent).mockResolvedValue({
      document_id: 7,
      title: "交换机手册",
      file_type: "txt",
      content: "# 这是一行普通文本",
      total_chars: 10,
      offset: 0,
      truncated: false,
    })

    render(
      <DocumentPreviewDrawer document={buildDocument("txt")} onClose={vi.fn()} />,
    )

    expect(await screen.findByText("# 这是一行普通文本")).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "这是一行普通文本" })).toBeNull()
  })

  it("截断时提示只预览了一部分", async () => {
    // 没有这个提示，用户会把片段当成全文
    vi.mocked(getDocumentContent).mockResolvedValue({
      document_id: 7,
      title: "交换机手册",
      file_type: "md",
      content: "前面一段",
      total_chars: 999_999,
      offset: 0,
      truncated: true,
    })

    render(
      <DocumentPreviewDrawer document={buildDocument("md")} onClose={vi.fn()} />,
    )

    expect(await screen.findByText(/仅预览前/)).toBeInTheDocument()
  })

  it("document 为 null 时不发请求", () => {
    render(<DocumentPreviewDrawer document={null} onClose={vi.fn()} />)
    expect(getDocumentContent).not.toHaveBeenCalled()
  })
})
