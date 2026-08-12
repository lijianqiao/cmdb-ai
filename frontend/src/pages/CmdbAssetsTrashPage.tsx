/** CMDB 资产回收站
 *
 * 结构照抄 UsersTrashPage.tsx：软删除资产的列表 + 恢复 + 永久删除。
 */

import { useMemo, useState } from "react"
import { Link } from "react-router"
import type { ColumnDef } from "@tanstack/react-table"
import dayjs from "dayjs"
import { toast } from "sonner"

import { Delete02Icon, Tick02Icon } from "@/lib/icons"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { PageHeader } from "@/components/layout/PageHeader"
import { DataTable } from "@/components/common/DataTable"
import { Pagination } from "@/components/common/Pagination"
import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import api from "@/lib/api"
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import { ROUTES } from "@/lib/constants"
import type { CmdbAsset } from "@/types/cmdb"

export function CmdbAssetsTrashPage() {
  const [search, setSearch] = useState("")
  const [purgeOpen, setPurgeOpen] = useState(false)
  const [purgeAsset, setPurgeAsset] = useState<CmdbAsset | null>(null)

  const {
    items: assets,
    total,
    page,
    setPage,
    pageSize,
    isLoading,
    onPageSizeChange,
    refetch: fetchDeleted,
  } = usePaginatedQuery<CmdbAsset>({
    url: "/cmdb/assets/deleted",
    params: search ? { search } : {},
    errorMessage: "获取回收站列表失败",
  })

  const handleRestore = async (asset: CmdbAsset) => {
    try {
      await api.post(`/cmdb/assets/${asset.id}/restore`)
      toast.success("恢复成功")
      fetchDeleted()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } }
      toast.error(error.response?.data?.message || "恢复失败")
    }
  }

  const handlePurgeClick = (asset: CmdbAsset) => {
    setPurgeAsset(asset)
    setPurgeOpen(true)
  }

  const handlePurgeConfirm = async (): Promise<boolean> => {
    if (!purgeAsset) return false
    try {
      await api.delete(`/cmdb/assets/${purgeAsset.id}/purge`)
      toast.success("已永久删除")
      fetchDeleted()
      return true
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } }
      toast.error(error.response?.data?.message || "永久删除失败")
      return false
    }
  }

  const columns = useMemo<ColumnDef<CmdbAsset>[]>(
    () => [
      {
        accessorKey: "hostname",
        header: "主机名",
        cell: ({ row }) => (
          <span className="font-medium">{row.original.hostname}</span>
        ),
      },
      { accessorKey: "ip_address", header: "IP 地址" },
      {
        accessorKey: "updated_at",
        header: "删除时间",
        cell: ({ row }) =>
          dayjs(row.original.updated_at).format("YYYY-MM-DD HH:mm"),
      },
      {
        id: "actions",
        header: "操作",
        cell: ({ row }) => (
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => handleRestore(row.original)}
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
    []
  )

  return (
    <div>
      <PageHeader
        title="CMDB 资产回收站"
        description="已删除的资产，可恢复或永久删除"
        actions={
          <Button variant="outline" render={<Link to={ROUTES.CMDB} />}>
            返回资产列表
          </Button>
        }
      />

      <div className="mb-4">
        <Input
          placeholder="搜索主机名或 IP..."
          value={search}
          onChange={(e) => {
            setSearch(e.target.value)
            setPage(1)
          }}
          className="sm:max-w-xs"
        />
      </div>

      <DataTable
        columns={columns}
        data={assets}
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
        description={`确定要永久删除资产「${purgeAsset?.hostname}」吗？此操作不可恢复。`}
        onConfirm={handlePurgeConfirm}
      />
    </div>
  )
}
