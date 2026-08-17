/** 监控日志页

 * DataTable + 筛选：按目标、状态与 IP/标签搜索。
 */

import { useMemo, useState } from "react"
import { useSearchParams } from "react-router"
import type { ColumnDef } from "@tanstack/react-table"
import dayjs from "dayjs"

import { Badge } from "@/components/ui/badge"
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
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import type { MonitorLatestStatus, MonitorLogItem } from "@/types/monitor"

const STATUS_LABELS: Record<MonitorLatestStatus, string> = {
  up: "在线",
  down: "离线",
}

/** base-ui 的 Select 需要 items 才能在受控赋值时渲染选中项文案 */
const STATUS_ITEMS = [
  { label: "全部状态", value: "all" },
  { label: "在线", value: "up" },
  { label: "离线", value: "down" },
]

function statusVariant(
  status: MonitorLatestStatus,
): "default" | "destructive" {
  return status === "up" ? "default" : "destructive"
}

function parseTargetId(value: string | null): number | undefined {
  if (!value) return undefined
  const parsed = Number.parseInt(value, 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined
}

export function MonitorLogsPage() {
  const [searchParams] = useSearchParams()
  const targetId = parseTargetId(searchParams.get("target_id"))
  const [statusFilter, setStatusFilter] = useState<string>("all")
  const [search, setSearch] = useState("")

  const {
    items: logs,
    total,
    page,
    setPage,
    pageSize,
    isLoading,
    onPageSizeChange,
  } = usePaginatedQuery<MonitorLogItem>({
    url: "/monitor/logs",
    params: {
      ...(targetId ? { target_id: targetId } : {}),
      ...(statusFilter !== "all" ? { status: statusFilter } : {}),
      ...(search ? { search } : {}),
    },
    initialPageSize: 20,
    errorMessage: "获取监控日志失败",
  })

  const columns = useMemo<ColumnDef<MonitorLogItem>[]>(
    () => [
      {
        accessorKey: "label",
        header: "标签",
        cell: ({ row }) => (
          <span
            className="block max-w-[180px] truncate font-medium"
            title={row.original.label || `${row.original.ip_address}:${row.original.port}`}
          >
            {row.original.label || `${row.original.ip_address}:${row.original.port}`}
          </span>
        ),
      },
      {
        id: "endpoint",
        header: "地址",
        cell: ({ row }) => (
          <span className="font-mono text-sm text-muted-foreground">
            {row.original.ip_address}:{row.original.port}
          </span>
        ),
      },
      {
        accessorKey: "status",
        header: "状态",
        cell: ({ row }) => (
          <Badge variant={statusVariant(row.original.status)}>
            {STATUS_LABELS[row.original.status]}
          </Badge>
        ),
      },
      {
        accessorKey: "latency_ms",
        header: "延迟",
        cell: ({ row }) => (
          <span className="font-mono text-sm">
            {row.original.latency_ms == null
              ? "—"
              : `${row.original.latency_ms} ms`}
          </span>
        ),
      },
      {
        accessorKey: "detail",
        header: "详情",
        cell: ({ row }) => (
          <span
            className="block max-w-xs line-clamp-2 text-sm text-muted-foreground"
            title={row.original.detail || undefined}
          >
            {row.original.detail || "-"}
          </span>
        ),
      },
      {
        accessorKey: "checked_at",
        header: "探测时间",
        cell: ({ row }) => (
          <span className="whitespace-nowrap text-xs text-muted-foreground">
            {dayjs(row.original.checked_at).format("YYYY-MM-DD HH:mm:ss")}
          </span>
        ),
      },
    ],
    [],
  )

  return (
    <div>
      <PageHeader
        title="监控日志"
        description={
          targetId
            ? `查看目标 #${targetId} 的探测历史记录`
            : "查看监控目标的状态变化与探测记录"
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
          items={STATUS_ITEMS}
          value={statusFilter}
          onValueChange={(value) => {
            setStatusFilter(value ?? "all")
            setPage(1)
          }}
        >
          <SelectTrigger className="sm:w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {STATUS_ITEMS.map((item) => (
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
        data={logs}
        isLoading={isLoading}
        emptyMessage="暂无监控日志"
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
