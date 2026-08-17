/** KnowledgePage 单测：分类筛选选择器默认渲染文案 */

// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, describe, expect, it, vi } from "vitest"

import { KnowledgePage } from "./KnowledgePage"

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
    render(<KnowledgePage />)

    // 等待并断言 Select 触发器渲染「全部分类」
    const triggers = await screen.findAllByRole("combobox")
    const categoryTrigger =
      triggers.find((el) => el.classList.contains("w-44")) ?? triggers[0]
    expect(categoryTrigger).toHaveTextContent("全部分类")
    expect(categoryTrigger).not.toHaveTextContent("__all__")
  })
})
