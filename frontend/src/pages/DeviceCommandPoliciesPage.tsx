/** 设备命令策略管理页
 *
 * DataTable + 新增/编辑/删除 + 回收站入口。命令名仅展示目录 key，
 * 不暴露原始设备命令字符串。
 */

import { useMemo, useState } from "react"
import { Link } from "react-router"
import type { ColumnDef } from "@tanstack/react-table"
import dayjs from "dayjs"
import { toast } from "sonner"

import {
  PlusSignIcon,
  PencilEdit02Icon,
  Delete02Icon,
  MoreHorizontalIcon,
  InboxIcon,
} from "@/lib/icons"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { PageHeader } from "@/components/layout/PageHeader"
import { DataTable } from "@/components/common/DataTable"
import { Pagination } from "@/components/common/Pagination"
import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { DeviceCommandPolicyFormDialog } from "@/components/device-command-policies/DeviceCommandPolicyFormDialog"
import api from "@/lib/api"
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import { usePermission } from "@/hooks/use-permission"
import { PERMISSIONS, ROUTES } from "@/lib/constants"
import type {
  DeviceCommandPolicy,
  DeviceCommandPolicyCreate,
  DeviceCommandPolicyUpdate,
} from "@/types/device-command-policy"

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
  if (policy.asset) {
    const typeLabel =
      ASSET_TYPE_LABELS[policy.asset.asset_type] ?? policy.asset.asset_type
    return `${policy.asset.hostname} (${policy.asset.ip_address}) · ${typeLabel}`
  }
  return `设备 #${policy.asset_id}`
}

export function DeviceCommandPoliciesPage() {
  const { hasPermission } = usePermission()

  const {
    items: policies,
    total,
    page,
    setPage,
    pageSize,
    isLoading,
    onPageSizeChange,
    refetch: fetchPolicies,
  } = usePaginatedQuery<DeviceCommandPolicy>({
    url: "/device-command-policies/policies",
    initialPageSize: 20,
    errorMessage: "获取策略列表失败",
  })

  const [formOpen, setFormOpen] = useState(false)
  const [editingPolicy, setEditingPolicy] = useState<DeviceCommandPolicy | null>(
    null
  )
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deletePolicy, setDeletePolicy] = useState<DeviceCommandPolicy | null>(
    null
  )

  const handleCreate = () => {
    setEditingPolicy(null)
    setFormOpen(true)
  }

  const handleEdit = (policy: DeviceCommandPolicy) => {
    setEditingPolicy(policy)
    setFormOpen(true)
  }

  const handleSubmit = async (
    data: DeviceCommandPolicyCreate | DeviceCommandPolicyUpdate
  ): Promise<boolean> => {
    try {
      if (editingPolicy) {
        await api.patch(
          `/device-command-policies/policies/${editingPolicy.id}`,
          data
        )
        toast.success("更新成功")
      } else {
        await api.post("/device-command-policies/policies", data)
        toast.success("创建成功")
      }
      fetchPolicies()
      return true
    } catch {
      toast.error(editingPolicy ? "更新失败" : "创建失败")
      return false
    }
  }

  const handleDeleteConfirm = async (): Promise<boolean> => {
    if (!deletePolicy) return false
    try {
      await api.delete(`/device-command-policies/policies/${deletePolicy.id}`)
      toast.success("删除成功")
      fetchPolicies()
      return true
    } catch {
      toast.error("删除失败")
      return false
    }
  }

  const columns = useMemo<ColumnDef<DeviceCommandPolicy>[]>(
    () => [
      {
        id: "target",
        header: "目标",
        cell: ({ row }) => {
          const policy = row.original
          if (policy.scope === "asset_type") {
            const label =
              ASSET_TYPE_LABELS[policy.asset_type ?? ""] ?? policy.asset_type
            return (
              <span
                className="block max-w-[200px] truncate font-medium text-foreground"
                title={`类型：${label}`}
              >
                类型：{label}
              </span>
            )
          }
          if (policy.asset) {
            const typeLabel =
              ASSET_TYPE_LABELS[policy.asset.asset_type] ?? policy.asset.asset_type
            return (
              <div
                className="flex flex-col min-w-0 max-w-[220px]"
                title={`${policy.asset.hostname} (${policy.asset.ip_address}) · ${typeLabel}`}
              >
                <span className="truncate font-medium text-foreground">
                  {policy.asset.hostname}
                </span>
                <span className="truncate font-mono text-xs text-muted-foreground">
                  {policy.asset.ip_address} · {typeLabel}
                </span>
              </div>
            )
          }
          return (
            <span
              className="block max-w-[180px] truncate font-medium text-foreground"
              title={`设备 #${policy.asset_id}`}
            >
              设备 #{policy.asset_id}
            </span>
          )
        },
      },
      {
        accessorKey: "command_name",
        header: "命令名",
        cell: ({ row }) => (
          <code
            className="inline-block max-w-[220px] truncate rounded bg-muted px-1.5 py-0.5 text-xs font-mono"
            title={row.original.command_name}
          >
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
            <Badge
              variant={isWhitelist ? "secondary" : "destructive"}
              className={
                isWhitelist
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 font-medium"
                  : undefined
              }
            >
              {isWhitelist ? "白名单" : "黑名单"}
            </Badge>
          )
        },
      },
      {
        accessorKey: "note",
        header: "备注",
        cell: ({ row }) => (
          <span
            className="block max-w-xs line-clamp-2 text-sm text-muted-foreground"
            title={row.original.note || undefined}
          >
            {row.original.note || "-"}
          </span>
        ),
      },
      {
        accessorKey: "created_at",
        header: "创建时间",
        cell: ({ row }) => (
          <span className="whitespace-nowrap text-xs text-muted-foreground">
            {dayjs(row.original.created_at).format("YYYY-MM-DD HH:mm")}
          </span>
        ),
      },
      {
        id: "actions",
        header: "操作",
        cell: ({ row }) => (
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button variant="ghost" size="icon-sm" aria-label="更多操作" />
              }
            >
              <MoreHorizontalIcon />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuGroup>
                {hasPermission(PERMISSIONS.DEVICE_COMMAND_POLICY_MANAGE) && (
                  <DropdownMenuItem onClick={() => handleEdit(row.original)}>
                    <PencilEdit02Icon />
                    <span>编辑</span>
                  </DropdownMenuItem>
                )}
                {hasPermission(PERMISSIONS.DEVICE_COMMAND_POLICY_MANAGE) && (
                  <DropdownMenuItem
                    onClick={() => {
                      setDeletePolicy(row.original)
                      setDeleteOpen(true)
                    }}
                    className="text-destructive"
                  >
                    <Delete02Icon />
                    <span>删除</span>
                  </DropdownMenuItem>
                )}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        ),
      },
    ],
    [hasPermission]
  )

  return (
    <div>
      <PageHeader
        title="设备命令策略"
        description="管理设备诊断命令的白名单与黑名单"
        actions={
          <>
            {hasPermission(PERMISSIONS.DEVICE_COMMAND_POLICY_MANAGE) && (
              <Button
                variant="outline"
                render={<Link to={ROUTES.DEVICE_COMMAND_POLICIES_TRASH} />}
              >
                <InboxIcon data-icon="inline-start" />
                回收站
              </Button>
            )}
            {hasPermission(PERMISSIONS.DEVICE_COMMAND_POLICY_MANAGE) && (
              <Button onClick={handleCreate}>
                <PlusSignIcon data-icon="inline-start" />
                新增策略
              </Button>
            )}
          </>
        }
      />

      <DataTable
        columns={columns}
        data={policies}
        isLoading={isLoading}
        emptyMessage="暂无策略数据"
      />

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={onPageSizeChange}
      />

      <DeviceCommandPolicyFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        policy={editingPolicy}
        onSubmit={handleSubmit}
      />

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="确认删除策略"
        description={`确定要删除策略「${deletePolicy ? formatTarget(deletePolicy) : ""} / ${deletePolicy?.command_name ?? ""}」吗？`}
        onConfirm={handleDeleteConfirm}
      />
    </div>
  )
}
