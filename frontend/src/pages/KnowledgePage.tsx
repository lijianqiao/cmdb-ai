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

import {
  Delete02Icon,
  MagicWand01Icon,
  Tick02Icon,
  Upload01Icon,
  ViewIcon,
} from "@/lib/icons"
import { DocumentPreviewDrawer } from "@/components/knowledge/DocumentPreviewDrawer"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { KnowledgeUploadDialog } from "@/components/ops-assistant/KnowledgeUploadDialog"
import { PageHeader } from "@/components/layout/PageHeader"
import { DataTable } from "@/components/common/DataTable"
import { Pagination } from "@/components/common/Pagination"
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import { usePermission } from "@/hooks/use-permission"
import { PERMISSIONS } from "@/lib/constants"
import { Link } from "react-router"

import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { ROUTES } from "@/lib/constants"
import {
  applyDocumentCategory,
  classifyDocuments,
  deleteDocument,
  listCategories,
  type KnowledgeCategory,
  type KnowledgeDocument,
} from "@/lib/knowledge-api"
import { useEffect } from "react"

const ALL_CATEGORIES = "__all__"
/** 后端 classify 接口单次上限，与 KnowledgeClassifyRequest 的 max_length 对齐 */
const MAX_CLASSIFY_BATCH = 50

/**
 * 这条建议可以应用吗？
 *
 * 行内「应用」按钮和「应用本页建议」必须用**同一个**判定，两边各写一套是先前
 * 那个「点了没反应」bug 的直接原因。
 *
 * 判定就是「有没有建议」：后端已经不再落库「建议 == 当前分类」的记录
 * （见 SuggestionOutcome.unchanged），所以只要存在建议，应用就一定会改变归属。
 * 早于该修复写入的历史行仍可能建议等于现分类，对它们点应用相当于确认并清除建议——
 * 也是合理的出口，总好过留一个永远灰着、又清不掉的死结。
 */
function isApplicableSuggestion(document: KnowledgeDocument): boolean {
  return document.suggested_category_id != null
}

export function KnowledgePage() {
  const { hasPermission } = usePermission()
  const canManage = hasPermission(PERMISSIONS.KNOWLEDGE_MANAGE)
  const canUploadKnowledge = hasPermission(PERMISSIONS.KNOWLEDGE_UPLOAD)

  const [uploadOpen, setUploadOpen] = useState(false)
  const [categories, setCategories] = useState<KnowledgeCategory[]>([])
  const [categoryFilter, setCategoryFilter] = useState<string>(ALL_CATEGORIES)
  const [pendingOnly, setPendingOnly] = useState(false)
  const [search, setSearch] = useState("")
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [isClassifying, setIsClassifying] = useState(false)
  const [previewDocument, setPreviewDocument] = useState<KnowledgeDocument | null>(
    null,
  )
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeDocument | null>(null)

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

  /** base-ui 的 Select 需要 items 才能在受控赋值时渲染选中项文案 */
  const categoryItems = useMemo(
    () => [
      { label: "全部分类", value: ALL_CATEGORIES },
      ...categories.map((category) => ({
        label: category.name,
        value: String(category.id),
      })),
    ],
    [categories],
  )

  const resetToFirstPage = () => {
    setPage(1)
    setSelectedIds([])
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
      // 三种「没产生建议」的原因必须分开说，它们对用户意味着完全不同的下一步：
      //   unchanged → 当前分类就是对的，什么都不用做
      //   no_match  → 现有分类都不合适，该去新建一个分类
      //   skipped   → 真的失败了（正文读不到 / 模型调用或解析出错），可以重试
      const details = [
        result.unchanged > 0 ? `${result.unchanged} 份维持原分类` : "",
        result.no_match > 0 ? `${result.no_match} 份没有合适的分类` : "",
        result.skipped > 0 ? `${result.skipped} 份分析失败` : "",
      ].filter(Boolean)
      const suffix = details.length > 0 ? `（${details.join("，")}）` : ""
      if (result.suggested > 0) {
        toast.success(`已生成 ${result.suggested} 份建议${suffix}`)
      } else if (result.no_match > 0) {
        toast.warning(
          `现有分类都不合适，建议先新建一个更贴切的分类再重试${suffix}`,
        )
      } else if (result.unchanged > 0) {
        toast.success(`当前分类已经是合适的，无需调整${suffix}`)
      } else {
        toast.error(`分析失败，请稍后重试${suffix}`)
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

  const handleDeleteConfirm = async (): Promise<boolean> => {
    if (!deleteTarget) return false
    try {
      await deleteDocument(deleteTarget.id)
      toast.success(`已把《${deleteTarget.title}》移入回收站`)
      await refetch()
      return true
    } catch {
      toast.error("删除失败")
      return false
    }
  }

  const suggestedDocuments = useMemo(
    () => documents.filter(isApplicableSuggestion),
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
          <div className="flex max-w-xs flex-col">
            <span className="truncate font-medium" title={row.original.title}>
              {row.original.title}
            </span>
            <span
              className="truncate text-xs text-muted-foreground"
              title={row.original.original_filename}
            >
              {row.original.original_filename}
            </span>
          </div>
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
            <div className="flex max-w-sm flex-col gap-0.5">
              <div className="flex items-center gap-1.5">
                <Badge className="max-w-[160px] truncate">
                  {categoryName.get(document.suggested_category_id) ??
                    `#${document.suggested_category_id}`}
                </Badge>
                {confidence != null ? (
                  <span className="text-xs text-muted-foreground">
                    {Math.round(confidence * 100)}%
                  </span>
                ) : null}
              </div>
              {document.suggestion_reason ? (
                <span
                  className="line-clamp-2 text-xs text-muted-foreground"
                  title={document.suggestion_reason}
                >
                  {document.suggestion_reason}
                </span>
              ) : null}
            </div>
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
          const canApply = canManage && isApplicableSuggestion(document)
          return (
            <div className="flex items-center gap-1.5">
              <Button
                type="button"
                size="xs"
                variant="ghost"
                aria-label={`预览 ${document.title}`}
                onClick={() => setPreviewDocument(document)}
              >
                <ViewIcon />
                预览
              </Button>
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
              {canManage ? (
                <Button
                  type="button"
                  size="xs"
                  variant="ghost"
                  aria-label={`删除 ${document.title}`}
                  onClick={() => setDeleteTarget(document)}
                >
                  <Delete02Icon />
                  删除
                </Button>
              ) : null}
            </div>
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
        actions={
          <div className="flex items-center gap-2">
            {canManage ? (
              <Button variant="outline" render={<Link to={ROUTES.KNOWLEDGE_TRASH} />}>
                <Delete02Icon data-icon="inline-start" />
                回收站
              </Button>
            ) : null}
            {canUploadKnowledge ? (
              <Button type="button" onClick={() => setUploadOpen(true)}>
                <Upload01Icon data-icon="inline-start" />
                上传知识文档
              </Button>
            ) : null}
          </div>
        }
      />

      <KnowledgeUploadDialog
        open={uploadOpen}
        onOpenChange={(open) => {
          setUploadOpen(open)
          if (!open) void refetch()
        }}
      />

      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={search}
          onChange={(event) => {
            setSearch(event.target.value)
            resetToFirstPage()
          }}
          placeholder="搜索标题或文件名..."
          className="w-56"
        />

        <Select
          items={categoryItems}
          value={categoryFilter}
          onValueChange={(value) => {
            // Base UI 的 Select 清空选择时会传 null，回落到「全部分类」
            setCategoryFilter(value ?? ALL_CATEGORIES)
            resetToFirstPage()
          }}
        >
          <SelectTrigger className="w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {categoryItems.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectGroup>
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
        emptyMessage="暂无知识文档，可点击右上角「上传知识文档」进行添加"
      />

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={onPageSizeChange}
      />

      <DocumentPreviewDrawer
        document={previewDocument}
        onClose={() => setPreviewDocument(null)}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null)
        }}
        title="确认删除"
        description={`确定要把《${deleteTarget?.title ?? ""}》移入回收站吗？移入后运维助手将不再检索到它，可在回收站恢复。`}
        onConfirm={handleDeleteConfirm}
      />
    </div>
  )
}

export default KnowledgePage
