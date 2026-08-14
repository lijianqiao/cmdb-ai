/** 设备命令策略回收站
 *
 * 结构照抄 CmdbAssetsTrashPage.tsx：软删除策略的列表 + 恢复 + 永久删除。
 */

import { useCallback, useMemo, useState } from "react"
import { Link } from "react-router"
import type { ColumnDef } from "@tanstack/react-table"
import dayjs from "dayjs"
import { toast } from "sonner"

import { Delete02Icon, Tick02Icon } from "@/lib/icons"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { PageHeader } from "@/components/layout/PageHeader"
import { DataTable } from "@/components/common/DataTable"
import { Pagination } from "@/components/common/Pagination"
import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import api from "@/lib/api"
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import { ROUTES } from "@/lib/constants"
import type { DeviceCommandPolicy } from "@/types/device-command-policy"

const ASSET_TYPE_LABELS: Record<string, string> = {
  server: "服务器",
  switch: "交换机",
  router: "路由器",
  firewall: "防火墙",
  load_balancer: "负载均衡",
  storage: "存储",
  other: "其他",
}

function formatTarget(policy: DeviceCommandPolicy): string {
  if (policy.scope === "asset_type") {
    const label =
      ASSET_TYPE_LABELS[policy.asset_type ?? ""] ?? policy.asset_type
    return `类型：${label}`
  }
  return `资产 #${policy.asset_id}`
}

export function DeviceCommandPoliciesTrashPage() {
  const [purgeOpen, setPurgeOpen] = useState(false)
  const [purgePolicy, setPurgePolicy] = useState<DeviceCommandPolicy | null>(
    null
  )

  const {
    items: policies,
    total,
    page,
    setPage,
    pageSize,
    isLoading,
    onPageSizeChange,
    refetch: fetchDeleted,
  } = usePaginatedQuery<DeviceCommandPolicy>({
    url: "/device-command-policies/policies/deleted",
    errorMessage: "获取回收站列表失败",
  })

  const handleRestore = useCallback(async (policy: DeviceCommandPolicy) => {
    try {
      await api.post(`/device-command-policies/policies/${policy.id}/restore`)
      toast.success("恢复成功")
      fetchDeleted()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } }
      toast.error(error.response?.data?.message || "恢复失败")
    }
  }, [fetchDeleted])

  const handlePurgeClick = (policy: DeviceCommandPolicy) => {
    setPurgePolicy(policy)
    setPurgeOpen(true)
  }

  const handlePurgeConfirm = async (): Promise<boolean> => {
    if (!purgePolicy) return false
    try {
      await api.delete(
        `/device-command-policies/policies/${purgePolicy.id}/purge`
      )
      toast.success("已永久删除")
      fetchDeleted()
      return true
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } }
      toast.error(error.response?.data?.message || "永久删除失败")
      return false
    }
  }

  const columns = useMemo<ColumnDef<DeviceCommandPolicy>[]>(
    () => [
      {
        id: "target",
        header: "目标",
        cell: ({ row }) => (
          <span className="font-medium">{formatTarget(row.original)}</span>
        ),
      },
      {
        accessorKey: "command_name",
        header: "命令名",
        cell: ({ row }) => (
          <code className="rounded bg-muted px-1.5 py-0.5 text-sm">
            {row.original.command_name}
          </code>
        ),
      },
      {
        accessorKey: "decision",
        header: "决定",
        cell: ({ row }) => {
          const isWhitelist = row.original.decision === "whitelist"
          return (
            <Badge variant={isWhitelist ? "default" : "destructive"}>
              {isWhitelist ? "白名单" : "黑名单"}
            </Badge>
          )
        },
      },
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
    [handleRestore]
  )

  return (
    <div>
      <PageHeader
        title="设备命令策略回收站"
        description="已删除的策略，可恢复或永久删除"
        actions={
          <Button
            variant="outline"
            render={<Link to={ROUTES.DEVICE_COMMAND_POLICIES} />}
          >
            返回策略列表
          </Button>
        }
      />

      <DataTable
        columns={columns}
        data={policies}
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
        description={`确定要永久删除策略「${purgePolicy ? formatTarget(purgePolicy) : ""} / ${purgePolicy?.command_name ?? ""}」吗？此操作不可恢复。`}
        onConfirm={handlePurgeConfirm}
      />
    </div>
  )
}
