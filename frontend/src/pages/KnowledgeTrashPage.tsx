/** 知识库回收站
 *
 * 结构照抄 DeviceCommandPoliciesTrashPage.tsx：软删除文档的列表 + 恢复 + 永久删除。
 */

import { useCallback, useMemo, useState } from "react"
import { Link } from "react-router"
import type { ColumnDef } from "@tanstack/react-table"
import dayjs from "dayjs"
import { toast } from "sonner"

import { Delete02Icon, Tick02Icon } from "@/lib/icons"
import { Button } from "@/components/ui/button"
import { PageHeader } from "@/components/layout/PageHeader"
import { DataTable } from "@/components/common/DataTable"
import { Pagination } from "@/components/common/Pagination"
import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import { ROUTES } from "@/lib/constants"
import {
  purgeDocument,
  restoreDocument,
  type KnowledgeDocument,
} from "@/lib/knowledge-api"

function readErrorMessage(error: unknown, fallback: string): string {
  const response = (error as { response?: { data?: { message?: string } } })
    .response
  return response?.data?.message || fallback
}

export function KnowledgeTrashPage() {
  const [purgeOpen, setPurgeOpen] = useState(false)
  const [purgeTarget, setPurgeTarget] = useState<KnowledgeDocument | null>(null)

  const {
    items: documents,
    total,
    page,
    setPage,
    pageSize,
    isLoading,
    onPageSizeChange,
    refetch: fetchDeleted,
  } = usePaginatedQuery<KnowledgeDocument>({
    url: "/knowledge/documents/deleted",
    errorMessage: "获取回收站列表失败",
  })

  const handleRestore = useCallback(
    async (document: KnowledgeDocument) => {
      try {
        await restoreDocument(document.id)
        toast.success("恢复成功")
        fetchDeleted()
      } catch (error) {
        // 同内容文档被重新上传时后端返回 409，详情里点名了冲突的那一份，
        // 直接透出比换成泛化文案有用
        toast.error(readErrorMessage(error, "恢复失败"))
      }
    },
    [fetchDeleted],
  )

  const handlePurgeClick = (document: KnowledgeDocument) => {
    setPurgeTarget(document)
    setPurgeOpen(true)
  }

  const handlePurgeConfirm = async (): Promise<boolean> => {
    if (!purgeTarget) return false
    try {
      await purgeDocument(purgeTarget.id)
      toast.success("已永久删除")
      fetchDeleted()
      return true
    } catch (error) {
      toast.error(readErrorMessage(error, "永久删除失败"))
      return false
    }
  }

  const columns = useMemo<ColumnDef<KnowledgeDocument>[]>(
    () => [
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
        accessorKey: "updated_at",
        header: "删除时间",
        cell: ({ row }) => (
          <span className="whitespace-nowrap text-xs text-muted-foreground">
            {dayjs(row.original.updated_at).format("YYYY-MM-DD HH:mm")}
          </span>
        ),
      },
      {
        id: "actions",
        header: "操作",
        cell: ({ row }) => (
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void handleRestore(row.original)}
            >
              <Tick02Icon data-icon="inline-start" />
              恢复
            </Button>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => handlePurgeClick(row.original)}
            >
              <Delete02Icon data-icon="inline-start" />
              永久删除
            </Button>
          </div>
        ),
      },
    ],
    [handleRestore],
  )

  return (
    <div>
      <PageHeader
        title="知识库回收站"
        description="已删除的知识文档，可恢复或永久删除；回收站中的文档不会被运维助手检索到"
        actions={
          <Button variant="outline" render={<Link to={ROUTES.KNOWLEDGE} />}>
            返回知识库
          </Button>
        }
      />

      <DataTable
        columns={columns}
        data={documents}
        isLoading={isLoading}
        emptyMessage="回收站为空"
      />

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={onPageSizeChange}
      />

      <ConfirmDialog
        open={purgeOpen}
        onOpenChange={setPurgeOpen}
        title="确认永久删除"
        description={`确定要永久删除《${purgeTarget?.title ?? ""}》吗？正文文件与向量切片会一并清除，此操作不可恢复。`}
        onConfirm={handlePurgeConfirm}
      />
    </div>
  )
}

export default KnowledgeTrashPage
