/** 知识库管理页
 *
 * 在此之前知识文档只能上传、无法查看，AI 分类建议也没有落脚的地方。
 * 这个页面补上三件事：看得到文档、按分类/关键词/待确认建议筛选、
 * 批量拿建议并一键应用。
 *
 * 关键约束：AI 只给建议，真实归属永远由人点「应用」才改变（后端也是这么设计的，
 * 见 services/knowledge_classification.py）。所以建议列单独展示、不与当前分类混排，
 * 用户一眼能看出「现在在哪」和「AI 觉得该在哪」。
 */

import { useMemo, useState } from "react"
import type { ColumnDef } from "@tanstack/react-table"
import dayjs from "dayjs"
import { toast } from "sonner"

import { MagicWand01Icon, Tick02Icon } from "@/lib/icons"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { PageHeader } from "@/components/layout/PageHeader"
import { DataTable } from "@/components/common/DataTable"
import { Pagination } from "@/components/common/Pagination"
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import { usePermission } from "@/hooks/use-permission"
import { PERMISSIONS } from "@/lib/constants"
import {
  applyDocumentCategory,
  classifyDocuments,
  listCategories,
  type KnowledgeCategory,
  type KnowledgeDocument,
} from "@/lib/knowledge-api"
import { useEffect } from "react"

const ALL_CATEGORIES = "__all__"
/** 后端 classify 接口单次上限，与 KnowledgeClassifyRequest 的 max_length 对齐 */
const MAX_CLASSIFY_BATCH = 50

export function KnowledgePage() {
  const { hasPermission } = usePermission()
  const canManage = hasPermission(PERMISSIONS.KNOWLEDGE_MANAGE)

  const [categories, setCategories] = useState<KnowledgeCategory[]>([])
  const [categoryFilter, setCategoryFilter] = useState<string>(ALL_CATEGORIES)
  const [pendingOnly, setPendingOnly] = useState(false)
  const [searchInput, setSearchInput] = useState("")
  const [search, setSearch] = useState("")
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [isClassifying, setIsClassifying] = useState(false)

  const params = useMemo(() => {
    const next: Record<string, unknown> = {}
    if (categoryFilter !== ALL_CATEGORIES) next.category_id = Number(categoryFilter)
    if (search) next.search = search
    if (pendingOnly) next.pending_suggestion = true
    return next
  }, [categoryFilter, search, pendingOnly])

  const {
    items: documents,
    total,
    page,
    setPage,
    pageSize,
    isLoading,
    onPageSizeChange,
    refetch,
  } = usePaginatedQuery<KnowledgeDocument>({
    url: "/knowledge/documents",
    params,
    errorMessage: "获取知识文档失败",
  })

  useEffect(() => {
    listCategories()
      .then(setCategories)
      .catch(() => toast.error("获取知识库分类失败"))
  }, [])

  const categoryName = useMemo(() => {
    const map = new Map<number, string>()
    for (const category of categories) map.set(category.id, category.name)
    return map
  }, [categories])

  const resetToFirstPage = () => {
    setPage(1)
    setSelectedIds([])
  }

  const handleSearch = () => {
    setSearch(searchInput.trim())
    resetToFirstPage()
  }

  const toggleSelected = (id: number) => {
    setSelectedIds((previous) =>
      previous.includes(id)
        ? previous.filter((item) => item !== id)
        : [...previous, id],
    )
  }

  const allOnPageSelected =
    documents.length > 0 && documents.every((item) => selectedIds.includes(item.id))

  const toggleSelectAllOnPage = () => {
    const pageIds = documents.map((item) => item.id)
    setSelectedIds((previous) =>
      allOnPageSelected
        ? previous.filter((id) => !pageIds.includes(id))
        : [...new Set([...previous, ...pageIds])],
    )
  }

  const handleClassify = async () => {
    if (selectedIds.length === 0) return
    if (selectedIds.length > MAX_CLASSIFY_BATCH) {
      toast.error(`单次最多分析 ${MAX_CLASSIFY_BATCH} 份文档`)
      return
    }
    setIsClassifying(true)
    try {
      const result = await classifyDocuments(selectedIds)
      if (result.suggested === 0) {
        toast.warning("没有生成任何建议，请检查分类是否已配置或稍后重试")
      } else {
        toast.success(
          `已生成 ${result.suggested} 份建议` +
            (result.skipped > 0 ? `，${result.skipped} 份未能给出建议` : ""),
        )
      }
      setSelectedIds([])
      await refetch()
    } catch {
      toast.error("生成分类建议失败")
    } finally {
      setIsClassifying(false)
    }
  }

  const handleApply = async (document: KnowledgeDocument) => {
    if (document.suggested_category_id == null) return
    try {
      await applyDocumentCategory(document.id, document.suggested_category_id)
      toast.success("已应用建议分类")
      await refetch()
    } catch {
      toast.error("应用建议失败")
    }
  }

  const suggestedDocuments = useMemo(
    () => documents.filter((item) => item.suggested_category_id != null),
    [documents],
  )

  const handleApplyAllOnPage = async () => {
    if (suggestedDocuments.length === 0) return
    // 串行应用：每次 PATCH 都是独立事务，一份失败不影响其它份，
    // 而且逐份提交能让用户在中途失败时保留已生效的部分。
    let applied = 0
    for (const document of suggestedDocuments) {
      try {
        await applyDocumentCategory(document.id, document.suggested_category_id!)
        applied += 1
      } catch {
        // 单份失败继续处理下一份，最后统一汇报
      }
    }
    if (applied === suggestedDocuments.length) {
      toast.success(`已应用 ${applied} 条建议`)
    } else {
      toast.warning(
        `应用了 ${applied} 条，${suggestedDocuments.length - applied} 条失败`,
      )
    }
    await refetch()
  }

  const columns = useMemo<ColumnDef<KnowledgeDocument>[]>(
    () => [
      {
        id: "select",
        header: () => (
          <Checkbox
            checked={allOnPageSelected}
            onCheckedChange={toggleSelectAllOnPage}
            aria-label="全选本页"
            disabled={!canManage || documents.length === 0}
          />
        ),
        cell: ({ row }) => (
          <Checkbox
            checked={selectedIds.includes(row.original.id)}
            onCheckedChange={() => toggleSelected(row.original.id)}
            aria-label={`选择 ${row.original.title}`}
            disabled={!canManage}
          />
        ),
      },
      {
        accessorKey: "title",
        header: "标题",
        cell: ({ row }) => (
          <span className="flex flex-col">
            <span className="font-medium">{row.original.title}</span>
            <span className="text-xs text-muted-foreground">
              {row.original.original_filename}
            </span>
          </span>
        ),
      },
      {
        accessorKey: "category_id",
        header: "当前分类",
        cell: ({ row }) => (
          <Badge variant="outline">
            {categoryName.get(row.original.category_id) ?? `#${row.original.category_id}`}
          </Badge>
        ),
      },
      {
        id: "suggestion",
        header: "AI 建议",
        cell: ({ row }) => {
          const document = row.original
          if (document.suggested_category_id == null) {
            return <span className="text-xs text-muted-foreground">—</span>
          }
          const confidence = document.suggestion_confidence
          return (
            <span className="flex flex-col gap-0.5">
              <span className="flex items-center gap-1">
                <Badge>
                  {categoryName.get(document.suggested_category_id) ??
                    `#${document.suggested_category_id}`}
                </Badge>
                {confidence != null ? (
                  <span className="text-xs text-muted-foreground">
                    {Math.round(confidence * 100)}%
                  </span>
                ) : null}
              </span>
              {document.suggestion_reason ? (
                <span className="text-xs text-muted-foreground">
                  {document.suggestion_reason}
                </span>
              ) : null}
            </span>
          )
        },
      },
      {
        accessorKey: "created_at",
        header: "上传时间",
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground">
            {dayjs(row.original.created_at).format("YYYY-MM-DD HH:mm")}
          </span>
        ),
      },
      {
        id: "actions",
        header: "操作",
        cell: ({ row }) => {
          const document = row.original
          const canApply =
            canManage &&
            document.suggested_category_id != null &&
            document.suggested_category_id !== document.category_id
          return (
            <Button
              type="button"
              size="xs"
              variant="outline"
              disabled={!canApply}
              onClick={() => void handleApply(document)}
            >
              <Tick02Icon />
              应用
            </Button>
          )
        },
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [documents, selectedIds, categoryName, canManage, allOnPageSelected],
  )

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="知识库"
        description="查看已上传的运维文档，按需生成 AI 分类建议并确认归类"
      />

      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") handleSearch()
          }}
          placeholder="搜索标题或文件名"
          className="w-56"
        />
        <Button type="button" variant="outline" onClick={handleSearch}>
          搜索
        </Button>

        <Select
          value={categoryFilter}
          onValueChange={(value) => {
            // Base UI 的 Select 清空选择时会传 null，回落到「全部分类」
            setCategoryFilter(value ?? ALL_CATEGORIES)
            resetToFirstPage()
          }}
        >
          <SelectTrigger className="w-44">
            <SelectValue placeholder="全部分类" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL_CATEGORIES}>全部分类</SelectItem>
            {categories.map((category) => (
              <SelectItem key={category.id} value={String(category.id)}>
                {category.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <label className="flex items-center gap-2 text-sm">
          <Checkbox
            checked={pendingOnly}
            onCheckedChange={(checked) => {
              setPendingOnly(checked === true)
              resetToFirstPage()
            }}
          />
          只看待确认建议
        </label>

        <span className="ml-auto flex items-center gap-2">
          {canManage ? (
            <>
              <Button
                type="button"
                variant="outline"
                disabled={selectedIds.length === 0 || isClassifying}
                onClick={() => void handleClassify()}
              >
                <MagicWand01Icon />
                {isClassifying
                  ? "分析中…"
                  : `AI 建议分类${selectedIds.length > 0 ? `（${selectedIds.length}）` : ""}`}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={suggestedDocuments.length === 0}
                onClick={() => void handleApplyAllOnPage()}
              >
                <Tick02Icon />
                应用本页建议（{suggestedDocuments.length}）
              </Button>
            </>
          ) : null}
        </span>
      </div>

      <DataTable
        columns={columns}
        data={documents}
        isLoading={isLoading}
        emptyMessage="暂无知识文档，可在运维助手页上传"
      />

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={onPageSizeChange}
      />
    </div>
  )
}

export default KnowledgePage
