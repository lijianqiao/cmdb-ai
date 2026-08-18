/** KnowledgePage 单测：分类筛选选择器默认渲染文案 */

// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, describe, expect, it, vi } from "vitest"

import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import {
  deleteDocument,
  getDocumentContent,
  type KnowledgeDocument,
} from "@/lib/knowledge-api"

import { MemoryRouter } from "react-router"

import { KnowledgePage } from "./KnowledgePage"

/** 页头有指向回收站的 Link，必须在 Router 上下文里渲染 */
function renderPage() {
  return render(
    <MemoryRouter>
      <KnowledgePage />
    </MemoryRouter>,
  )
}

function buildDocument(
  overrides: Partial<KnowledgeDocument> & Pick<KnowledgeDocument, "id">,
): KnowledgeDocument {
  return {
    category_id: 1,
    title: "文档",
    original_filename: "doc.md",
    file_path: "network/1_doc.md",
    file_type: "md",
    status: "ready",
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:00Z",
    suggested_category_id: null,
    suggestion_confidence: null,
    suggestion_reason: "",
    suggested_at: null,
    ...overrides,
  } as KnowledgeDocument
}

vi.mock("@/hooks/use-permission", () => ({
  usePermission: vi.fn(() => ({
    permissions: ["knowledge:manage"],
    hasPermission: () => true,
    hasAnyPermission: () => true,
    hasAllPermissions: () => true,
  })),
}))

vi.mock("@/hooks/use-paginated-query", () => ({
  usePaginatedQuery: vi.fn(() => ({
    items: [],
    total: 0,
    page: 1,
    setPage: vi.fn(),
    pageSize: 10,
    isLoading: false,
    onPageSizeChange: vi.fn(),
    refetch: vi.fn(),
  })),
}))

vi.mock("@/lib/knowledge-api", () => ({
  listCategories: vi.fn().mockResolvedValue([
    { id: 1, code: "network", name: "网络配置", created_at: "2026-08-01" },
    { id: 2, code: "system", name: "系统运维", created_at: "2026-08-01" },
  ]),
  classifyDocuments: vi.fn(),
  applyDocumentCategory: vi.fn(),
  getDocumentContent: vi.fn(),
  deleteDocument: vi.fn(),
}))

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

afterEach(() => {
  cleanup()
})

describe("KnowledgePage", () => {
  it("分类筛选框默认展示「全部分类」而不是「__all__」", async () => {
    renderPage()

    // 等待并断言 Select 触发器渲染「全部分类」
    const triggers = await screen.findAllByRole("combobox")
    const categoryTrigger =
      triggers.find((el) => el.classList.contains("w-44")) ?? triggers[0]
    expect(categoryTrigger).toHaveTextContent("全部分类")
    expect(categoryTrigger).not.toHaveTextContent("__all__")
  })

  it("批量计数与行内「应用」按钮的可用性完全一致", async () => {
    // 这两处先前各写了一套判定，批量把「建议 == 当前分类」也算进计数，
    // 点下去每份都被 PATCH 成它本来就在的分类：分类没变、建议却被清空了。
    // 现在共用 isApplicableSuggestion，这条断言把「两边必须一致」钉死。
    vi.mocked(usePaginatedQuery).mockReturnValue({
      items: [
        buildDocument({ id: 1, category_id: 1, suggested_category_id: 2 }),
        buildDocument({ id: 2, category_id: 1, suggested_category_id: 3 }),
        buildDocument({ id: 3, category_id: 1, suggested_category_id: null }),
      ],
      total: 3,
      page: 1,
      setPage: vi.fn(),
      pageSize: 10,
      isLoading: false,
      onPageSizeChange: vi.fn(),
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof usePaginatedQuery>)

    renderPage()

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /应用本页建议（2）/ }),
      ).toBeInTheDocument(),
    )
    const applyButtons = screen
      .getAllByRole("button", { name: "应用" })
      .filter((button) => !button.hasAttribute("disabled"))
    expect(applyButtons).toHaveLength(2)
  })

  it("列表每一行都提供预览入口", async () => {
    vi.mocked(usePaginatedQuery).mockReturnValue({
      items: [buildDocument({ id: 7, title: "交换机手册" })],
      total: 1,
      page: 1,
      setPage: vi.fn(),
      pageSize: 10,
      isLoading: false,
      onPageSizeChange: vi.fn(),
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof usePaginatedQuery>)

    vi.mocked(getDocumentContent).mockResolvedValue({
      document_id: 7,
      title: "交换机手册",
      file_type: "md",
      content: "正文段落",
      total_chars: 4,
      offset: 0,
      truncated: false,
    })

    renderPage()

    // 必须每次重新查询：listCategories 的 effect 落地会让整页重渲染，
    // 先 findBy 拿到的节点这时已经脱离文档，点它不会触发任何 handler
    await waitFor(() =>
      expect(screen.getByLabelText("预览 交换机手册")).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByLabelText("预览 交换机手册"))

    await waitFor(() => expect(getDocumentContent).toHaveBeenCalledWith(7))
  })

  it("每行提供删除入口，且删除要先确认", async () => {
    vi.mocked(usePaginatedQuery).mockReturnValue({
      items: [buildDocument({ id: 9, title: "要删的文档" })],
      total: 1,
      page: 1,
      setPage: vi.fn(),
      pageSize: 10,
      isLoading: false,
      onPageSizeChange: vi.fn(),
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof usePaginatedQuery>)

    renderPage()

    await waitFor(() =>
      expect(screen.getByLabelText("删除 要删的文档")).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByLabelText("删除 要删的文档"))

    // 先出确认弹窗，不能点一下就删
    expect(await screen.findByText(/移入回收站吗/)).toBeInTheDocument()
    expect(deleteDocument).not.toHaveBeenCalled()
  })
})
