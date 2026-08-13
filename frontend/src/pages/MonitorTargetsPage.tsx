/** 监控目标管理页
 *
 * DataTable + 搜索/启用筛选 + 新增/编辑/硬删除。
 * 没有回收站：删除会连带探测记录一起清掉。
 */

import { useMemo, useState } from "react"
import type { ColumnDef } from "@tanstack/react-table"
import dayjs from "dayjs"
import { toast } from "sonner"

import {
  PlusSignIcon,
  PencilEdit02Icon,
  Delete02Icon,
  MoreHorizontalIcon,
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
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { PageHeader } from "@/components/layout/PageHeader"
import { DataTable } from "@/components/common/DataTable"
import { Pagination } from "@/components/common/Pagination"
import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { MonitorTargetFormDialog } from "@/components/monitor/MonitorTargetFormDialog"
import api from "@/lib/api"
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import { usePermission } from "@/hooks/use-permission"
import { PERMISSIONS } from "@/lib/constants"
import type {
  MonitorTarget,
  MonitorTargetCreate,
  MonitorTargetUpdate,
} from "@/types/monitor"

const ACTIVE_FILTER_ITEMS = [
  { label: "全部状态", value: "all" },
  { label: "仅启用", value: "true" },
  { label: "仅停用", value: "false" },
] as const

function statusLabel(target: MonitorTarget): string {
  if (target.latest_status === "up") return "在线"
  if (target.latest_status === "down") return "离线"
  return "未探测"
}

export function MonitorTargetsPage() {
  const { hasPermission } = usePermission()
  const [search, setSearch] = useState("")
  const [activeFilter, setActiveFilter] = useState<"all" | "true" | "false">(
    "all",
  )

  const listParams: Record<string, unknown> = {}
  if (search) listParams.search = search
  if (activeFilter !== "all") listParams.is_active = activeFilter === "true"

  const {
    items: targets,
    total,
    page,
    setPage,
    pageSize,
    isLoading,
    onPageSizeChange,
    refetch: fetchTargets,
  } = usePaginatedQuery<MonitorTarget>({
    url: "/monitor/targets",
    params: listParams,
    errorMessage: "获取监控目标失败",
  })

  const [formOpen, setFormOpen] = useState(false)
  const [editingTarget, setEditingTarget] = useState<MonitorTarget | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<MonitorTarget | null>(null)

  const handleCreate = () => {
    setEditingTarget(null)
    setFormOpen(true)
  }

  const handleEdit = (target: MonitorTarget) => {
    setEditingTarget(target)
    setFormOpen(true)
  }

  const handleDeleteClick = (target: MonitorTarget) => {
    setDeleteTarget(target)
    setDeleteOpen(true)
  }

  const handleSubmit = async (
    data: MonitorTargetCreate | MonitorTargetUpdate,
  ): Promise<boolean> => {
    try {
      if (editingTarget) {
        await api.patch(`/monitor/targets/${editingTarget.id}`, data)
        toast.success("更新成功")
      } else {
        await api.post("/monitor/targets", data)
        toast.success("创建成功")
      }
      fetchTargets()
      return true
    } catch {
      toast.error(editingTarget ? "更新失败" : "创建失败")
      return false
    }
  }

  const handleDeleteConfirm = async (): Promise<boolean> => {
    if (!deleteTarget) return false
    try {
      await api.delete(`/monitor/targets/${deleteTarget.id}`)
      toast.success("删除成功")
      fetchTargets()
      return true
    } catch {
      toast.error("删除失败")
      return false
    }
  }

  const columns = useMemo<ColumnDef<MonitorTarget>[]>(
    () => [
      {
        accessorKey: "label",
        header: "标签",
        cell: ({ row }) => row.original.label || "-",
      },
      { accessorKey: "ip_address", header: "IP 地址" },
      { accessorKey: "port", header: "端口" },
      {
        accessorKey: "is_active",
        header: "启用",
        cell: ({ row }) => (
          <Badge variant={row.original.is_active ? "default" : "secondary"}>
            {row.original.is_active ? "启用" : "停用"}
          </Badge>
        ),
      },
      {
        id: "latest_status",
        header: "最近状态",
        cell: ({ row }) => {
          const target = row.original
          const variant =
            target.latest_status === "up"
              ? "default"
              : target.latest_status === "down"
                ? "destructive"
                : "secondary"
          return <Badge variant={variant}>{statusLabel(target)}</Badge>
        },
      },
      {
        id: "latest_latency_ms",
        header: "延迟",
        cell: ({ row }) =>
          row.original.latest_latency_ms == null
            ? "-"
            : `${row.original.latest_latency_ms} ms`,
      },
      {
        id: "latest_checked_at",
        header: "最近探测",
        cell: ({ row }) =>
          row.original.latest_checked_at
            ? dayjs(row.original.latest_checked_at).format("YYYY-MM-DD HH:mm")
            : "-",
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
                {hasPermission(PERMISSIONS.MONITOR_MANAGE) && (
                  <DropdownMenuItem onClick={() => handleEdit(row.original)}>
                    <PencilEdit02Icon />
                    <span>编辑</span>
                  </DropdownMenuItem>
                )}
                {hasPermission(PERMISSIONS.MONITOR_MANAGE) && (
                  <DropdownMenuItem
                    onClick={() => handleDeleteClick(row.original)}
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
    [hasPermission],
  )

  return (
    <div>
      <PageHeader
        title="监控目标"
        description="登记 TCP 探活地址；启用后由后台按系统配置的间隔巡检"
        actions={
          hasPermission(PERMISSIONS.MONITOR_MANAGE) ? (
            <Button onClick={handleCreate}>
              <PlusSignIcon data-icon="inline-start" />
              新增目标
            </Button>
          ) : null
        }
      />

      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <Input
          placeholder="搜索 IP 或标签..."
          value={search}
          onChange={(e) => {
            setSearch(e.target.value)
            setPage(1)
          }}
          className="sm:max-w-xs"
        />
        <Select
          items={[...ACTIVE_FILTER_ITEMS]}
          value={activeFilter}
          onValueChange={(value) => {
            if (value === "all" || value === "true" || value === "false") {
              setActiveFilter(value)
              setPage(1)
            }
          }}
        >
          <SelectTrigger className="sm:w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {ACTIVE_FILTER_ITEMS.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      </div>

      <DataTable
        columns={columns}
        data={targets}
        isLoading={isLoading}
        emptyMessage="暂无监控目标，请先新增"
      />

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={onPageSizeChange}
      />

      <MonitorTargetFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        target={editingTarget}
        onSubmit={handleSubmit}
      />
      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="确认删除监控目标"
        description={`确定要删除「${deleteTarget?.label || deleteTarget?.ip_address}」吗？探测记录会一并删除，且无法恢复。`}
        onConfirm={handleDeleteConfirm}
      />
    </div>
  )
}
