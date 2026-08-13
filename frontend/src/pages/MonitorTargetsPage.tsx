/** 监控目标管理页
 *
 * 卡片网格：一台设备一张卡。固定信息（IP/端口/标签/启用）不轮询；
 * 状态、延迟、最近探测按系统配置的巡检间隔静默刷新。
 */

import { useEffect, useMemo, useState } from "react"
import { useSearchParams } from "react-router"
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
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { PageHeader } from "@/components/layout/PageHeader"
import { Pagination } from "@/components/common/Pagination"
import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { MonitorTargetFormDialog } from "@/components/monitor/MonitorTargetFormDialog"
import api from "@/lib/api"
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import { usePermission } from "@/hooks/use-permission"
import { PERMISSIONS } from "@/lib/constants"
import type {
  MonitorRuntime,
  MonitorTarget,
  MonitorTargetCreate,
  MonitorTargetUpdate,
} from "@/types/monitor"
import type { ApiResponse, PaginatedData } from "@/types/api"

const DEFAULT_SWEEP_MS = 30_000

type ActiveFilter = "all" | "true" | "false"

interface MonitorLiveFields {
  latest_status: MonitorTarget["latest_status"]
  latest_latency_ms: MonitorTarget["latest_latency_ms"]
  latest_checked_at: MonitorTarget["latest_checked_at"]
}

function statusLabel(status: MonitorTarget["latest_status"]): string {
  if (status === "up") return "在线"
  if (status === "down") return "离线"
  return "未探测"
}

function statusVariant(
  status: MonitorTarget["latest_status"],
): "default" | "destructive" | "secondary" {
  if (status === "up") return "default"
  if (status === "down") return "destructive"
  return "secondary"
}

export function MonitorTargetsPage() {
  const { hasPermission } = usePermission()
  const [searchParams, setSearchParams] = useSearchParams()
  const [search, setSearch] = useState("")
  const [activeFilter, setActiveFilter] = useState<ActiveFilter>("all")
  const [liveById, setLiveById] = useState<Record<number, MonitorLiveFields>>(
    {},
  )
  const [sweepIntervalMs, setSweepIntervalMs] = useState(DEFAULT_SWEEP_MS)

  const listParams: Record<string, unknown> = {}
  if (search) listParams.search = search
  if (activeFilter !== "all") listParams.is_active = activeFilter === "true"
  const paramsKey = JSON.stringify(listParams)

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

  const canManage = hasPermission(PERMISSIONS.MONITOR_MANAGE)

  useEffect(() => {
    if (
      searchParams.get("create") === "1" &&
      hasPermission(PERMISSIONS.MONITOR_MANAGE)
    ) {
      setEditingTarget(null)
      setFormOpen(true)
      const next = new URLSearchParams(searchParams)
      next.delete("create")
      setSearchParams(next, { replace: true })
    }
  }, [searchParams, setSearchParams, hasPermission])

  useEffect(() => {
    let cancelled = false
    const loadRuntime = async () => {
      try {
        const response = await api.get<ApiResponse<MonitorRuntime>>(
          "/monitor/runtime",
        )
        const seconds = Number(response.data.data?.sweep_interval_seconds)
        if (!cancelled && Number.isFinite(seconds) && seconds >= 5) {
          setSweepIntervalMs(Math.round(seconds * 1000))
        }
      } catch {
        // 没有权限或接口失败时沿用 30 秒，避免打断列表页
      }
    }
    void loadRuntime()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const pollLive = async () => {
      try {
        const response = await api.get<ApiResponse<PaginatedData<MonitorTarget>>>(
          "/monitor/targets",
          {
            params: {
              ...(JSON.parse(paramsKey) as Record<string, unknown>),
              page,
              page_size: pageSize,
            },
          },
        )
        const items = response.data.data?.items ?? []
        setLiveById((prev) => {
          const next = { ...prev }
          for (const item of items) {
            next[item.id] = {
              latest_status: item.latest_status,
              latest_latency_ms: item.latest_latency_ms,
              latest_checked_at: item.latest_checked_at,
            }
          }
          return next
        })
      } catch {
        // 轮询失败不弹 toast，避免每隔巡检间隔刷屏
      }
    }

    const timer = window.setInterval(() => {
      void pollLive()
    }, sweepIntervalMs)
    return () => window.clearInterval(timer)
  }, [sweepIntervalMs, page, pageSize, paramsKey])

  const displayTargets = useMemo(
    () =>
      targets.map((target) => {
        const live = liveById[target.id]
        return live ? { ...target, ...live } : target
      }),
    [targets, liveById],
  )

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

  return (
    <div>
      <PageHeader
        title="监控目标"
        description="登记 TCP 探活地址；状态、延迟、最近探测按系统配置的巡检间隔刷新"
        actions={
          canManage ? (
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
        <ToggleGroup
          variant="outline"
          spacing={0}
          value={[activeFilter]}
          onValueChange={(value) => {
            const next = value[0]
            if (next === "all" || next === "true" || next === "false") {
              setActiveFilter(next)
              setPage(1)
            }
          }}
        >
          <ToggleGroupItem value="all">全部</ToggleGroupItem>
          <ToggleGroupItem value="true">仅启用</ToggleGroupItem>
          <ToggleGroupItem value="false">仅停用</ToggleGroupItem>
        </ToggleGroup>
      </div>

      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <Card key={index}>
              <CardHeader>
                <Skeleton className="h-5 w-40" />
                <Skeleton className="h-4 w-28" />
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                <Skeleton className="h-5 w-16" />
                <Skeleton className="h-12 w-full" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : displayTargets.length === 0 ? (
        <Empty className="border">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <InboxIcon />
            </EmptyMedia>
            <EmptyTitle>暂无监控目标</EmptyTitle>
            <EmptyDescription>
              {canManage ? "请先新增一台要探测的设备。" : "当前没有可查看的监控目标。"}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {displayTargets.map((target) => (
            <Card key={target.id}>
              <CardHeader>
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <CardTitle className="truncate">
                      {target.label || `${target.ip_address}:${target.port}`}
                    </CardTitle>
                    <CardDescription className="truncate">
                      {target.ip_address}:{target.port}
                    </CardDescription>
                  </div>
                  {canManage ? (
                    <DropdownMenu>
                      <DropdownMenuTrigger
                        render={
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label="更多操作"
                          />
                        }
                      >
                        <MoreHorizontalIcon />
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuGroup>
                          <DropdownMenuItem onClick={() => handleEdit(target)}>
                            <PencilEdit02Icon />
                            <span>编辑</span>
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            onClick={() => handleDeleteClick(target)}
                            className="text-destructive"
                          >
                            <Delete02Icon />
                            <span>删除</span>
                          </DropdownMenuItem>
                        </DropdownMenuGroup>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  ) : null}
                </div>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={target.is_active ? "default" : "secondary"}>
                    {target.is_active ? "启用" : "停用"}
                  </Badge>
                  {target.cmdb_asset_id != null ? (
                    <Badge variant="outline">
                      已关联资产 #{target.cmdb_asset_id}
                    </Badge>
                  ) : null}
                </div>
                <Separator />
                <div className="grid grid-cols-3 gap-2">
                  <div className="flex flex-col gap-1">
                    <p className="text-xs text-muted-foreground">状态</p>
                    <Badge variant={statusVariant(target.latest_status)}>
                      {statusLabel(target.latest_status)}
                    </Badge>
                  </div>
                  <div className="flex flex-col gap-1">
                    <p className="text-xs text-muted-foreground">延迟</p>
                    <p className="text-sm font-medium">
                      {target.latest_latency_ms == null
                        ? "—"
                        : `${target.latest_latency_ms} ms`}
                    </p>
                  </div>
                  <div className="flex min-w-0 flex-col gap-1">
                    <p className="text-xs text-muted-foreground">最近探测</p>
                    <p className="truncate text-sm font-medium">
                      {target.latest_checked_at
                        ? dayjs(target.latest_checked_at).format(
                            "MM-DD HH:mm",
                          )
                        : "—"}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

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
