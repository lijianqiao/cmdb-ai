/** 审计日志页

 * DataTable + 筛选。
 */

import { useMemo, useState } from "react"
import type { ColumnDef } from "@tanstack/react-table"
import dayjs from "dayjs"
import { toast } from "sonner"

import { AuditIcon, Copy01Icon, ViewIcon } from "@/lib/icons"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Drawer,
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerFooter,
  DrawerHeader,
  DrawerTitle,
} from "@/components/ui/drawer"
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
import type { AuditLog } from "@/types/audit"

const ACTION_LABELS: Record<string, string> = {
  register: "注册",
  login: "登录",
  logout: "退出",
  create_user: "创建用户",
  update_user: "更新用户",
  delete_user: "删除用户",
  assign_roles: "分配角色",
  create_role: "创建角色",
  update_role: "更新角色",
  delete_role: "删除角色",
  assign_permissions: "分配权限",
  create_permission: "创建权限",
  update_permission: "更新权限",
  delete_permission: "删除权限",
  restore_user: "恢复用户",
  purge_user: "永久删除用户",
  restore_role: "恢复角色",
  purge_role: "永久删除角色",
  restore_permission: "恢复权限",
  purge_permission: "永久删除权限",
  update_profile: "更新资料",
  change_password: "修改密码",
  reset_password: "重置密码",
  update_llm_system_config: "更新模型配置",
  update_operations_system_config: "更新运行配置",
  bootstrap_system_configs: "初始化运行配置",
  view_cmdb_credential: "查看资产凭据",
  update_session_approval_mode: "变更会话审批模式",
}

/** base-ui 的 Select 需要 items 才能在受控赋值时渲染选中项文案 */
const ACTION_ITEMS = [
  { label: "全部操作", value: "all" },
  ...Object.entries(ACTION_LABELS).map(([value, label]) => ({ label, value })),
]

function formatDetail(detail?: string | null): string {
  if (!detail) return "无详细记录"
  try {
    const parsed = JSON.parse(detail)
    return JSON.stringify(parsed, null, 2)
  } catch {
    return detail
  }
}

export function AuditLogsPage() {
  const [actionFilter, setActionFilter] = useState<string>("all")
  const [searchUsername, setSearchUsername] = useState("")
  const [selectedLog, setSelectedLog] = useState<AuditLog | null>(null)

  const {
    items: logs,
    total,
    page,
    setPage,
    pageSize,
    isLoading,
    onPageSizeChange,
  } = usePaginatedQuery<AuditLog>({
    url: "/audit-logs",
    params: {
      ...(actionFilter !== "all" ? { action: actionFilter } : {}),
      ...(searchUsername ? { username: searchUsername } : {}),
    },
    initialPageSize: 20,
    errorMessage: "获取审计日志失败",
  })

  const columns = useMemo<ColumnDef<AuditLog>[]>(
    () => [
      {
        accessorKey: "username",
        header: "用户",
        cell: ({ row }) => (
          <span className="font-medium">{row.original.username || "未知"}</span>
        ),
      },
      {
        accessorKey: "action",
        header: "操作",
        cell: ({ row }) => (
          <Badge variant="outline">
            {ACTION_LABELS[row.original.action] ?? row.original.action}
          </Badge>
        ),
      },
      {
        accessorKey: "target",
        header: "目标",
        cell: ({ row }) => (
          <span
            className="block max-w-xs truncate font-mono text-sm text-muted-foreground"
            title={row.original.target || undefined}
          >
            {row.original.target || "-"}
          </span>
        ),
      },
      {
        accessorKey: "detail",
        header: "详情",
        cell: ({ row }) => (
          <span
            className="block max-w-md line-clamp-2 text-sm text-muted-foreground"
            title={row.original.detail || undefined}
          >
            {row.original.detail || "-"}
          </span>
        ),
      },
      {
        accessorKey: "ip",
        header: "IP 地址",
        cell: ({ row }) => (
          <span className="font-mono text-sm">{row.original.ip || "-"}</span>
        ),
      },
      {
        accessorKey: "created_at",
        header: "时间",
        cell: ({ row }) => (
          <span className="whitespace-nowrap text-xs text-muted-foreground">
            {dayjs(row.original.created_at).format("YYYY-MM-DD HH:mm:ss")}
          </span>
        ),
      },
      {
        id: "actions",
        header: "操作",
        cell: ({ row }) => (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="查看详情"
            title="查看完整操作详情"
            onClick={() => setSelectedLog(row.original)}
          >
            <ViewIcon />
          </Button>
        ),
      },
    ],
    []
  )

  return (
    <div>
      <PageHeader title="操作日志" description="查看系统操作记录" />

      {/* 工具栏 */}
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <Input
          placeholder="按用户名筛选..."
          value={searchUsername}
          onChange={(e) => {
            setSearchUsername(e.target.value)
            setPage(1)
          }}
          className="sm:max-w-xs"
        />
        <Select
          items={ACTION_ITEMS}
          value={actionFilter}
          onValueChange={(value) => {
            setActionFilter(value ?? "all")
            setPage(1)
          }}
        >
          <SelectTrigger className="sm:w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {ACTION_ITEMS.map((item) => (
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
        emptyMessage="暂无日志记录"
      />

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={onPageSizeChange}
      />

      {/* 右侧日志详情抽屉 */}
      <Drawer
        swipeDirection="right"
        open={selectedLog !== null}
        onOpenChange={(open) => {
          if (!open) setSelectedLog(null)
        }}
      >
        <DrawerContent className="w-[88vw] sm:w-[420px] md:w-[460px] max-w-full overflow-x-hidden">
          <DrawerHeader className="border-b pb-4">
            <div className="flex items-center gap-2">
              <div className="flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <AuditIcon />
              </div>
              <DrawerTitle className="text-base font-semibold">
                操作日志详情
              </DrawerTitle>
            </div>
            <DrawerDescription>
              记录 ID #{selectedLog?.id} · {selectedLog ? dayjs(selectedLog.created_at).format("YYYY-MM-DD HH:mm:ss") : ""}
            </DrawerDescription>
          </DrawerHeader>

          <div className="flex flex-1 flex-col gap-4 overflow-y-auto overflow-x-hidden p-4 text-sm">
            {/* 完整属性明细列表 */}
            <div className="flex flex-col rounded-xl border bg-muted/20 divide-y divide-border/60">
              <div className="flex items-center justify-between p-3">
                <span className="text-xs text-muted-foreground">日志 ID</span>
                <span className="font-mono text-xs font-semibold text-foreground">
                  #{selectedLog?.id}
                </span>
              </div>

              <div className="flex items-center justify-between p-3">
                <span className="text-xs text-muted-foreground">操作用户</span>
                <div className="flex items-center gap-1.5 min-w-0">
                  <span className="font-medium text-foreground truncate max-w-[200px]">
                    {selectedLog?.username || "系统/未知"}
                  </span>
                  {selectedLog?.user_id != null && (
                    <span className="font-mono text-xs text-muted-foreground">
                      (UID: #{selectedLog.user_id})
                    </span>
                  )}
                </div>
              </div>

              <div className="flex items-start justify-between p-3 gap-2">
                <span className="text-xs text-muted-foreground shrink-0 pt-0.5">
                  操作行为
                </span>
                <div className="flex flex-col items-end gap-1 text-right min-w-0">
                  <Badge variant="outline" className="font-medium whitespace-normal break-all text-right">
                    {selectedLog
                      ? ACTION_LABELS[selectedLog.action] ?? selectedLog.action
                      : ""}
                  </Badge>
                  {selectedLog?.action && (
                    <code className="text-[11px] font-mono text-muted-foreground bg-muted px-1.5 py-0.5 rounded break-all max-w-[260px]">
                      {selectedLog.action}
                    </code>
                  )}
                </div>
              </div>

              <div className="flex items-center justify-between p-3">
                <span className="text-xs text-muted-foreground">来源 IP</span>
                <span className="font-mono text-xs text-foreground bg-muted/60 px-1.5 py-0.5 rounded">
                  {selectedLog?.ip || "-"}
                </span>
              </div>

              <div className="flex items-center justify-between p-3">
                <span className="text-xs text-muted-foreground">创建时间</span>
                <span className="font-mono text-xs text-foreground">
                  {selectedLog?.created_at
                    ? dayjs(selectedLog.created_at).format("YYYY-MM-DD HH:mm:ss")
                    : "-"}
                </span>
              </div>
            </div>

            {/* 操作目标 */}
            <div className="flex flex-col gap-1.5 rounded-xl border bg-card p-3.5">
              <span className="text-xs font-medium text-muted-foreground">
                操作目标 (Target)
              </span>
              <code className="rounded-lg bg-muted/60 p-2.5 font-mono text-xs text-foreground break-all whitespace-pre-wrap">
                {selectedLog?.target || "—"}
              </code>
            </div>

            {/* 详细记录 / 载荷 */}
            <div className="flex flex-1 flex-col gap-1.5 rounded-xl border bg-card p-3.5 min-h-0">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-muted-foreground">
                  详细记录 (Detail)
                </span>
                {selectedLog?.detail && (
                  <Button
                    variant="ghost"
                    size="xs"
                    onClick={() => {
                      void navigator.clipboard.writeText(selectedLog.detail)
                      toast.success("已复制详情到剪贴板")
                    }}
                  >
                    <Copy01Icon data-icon="inline-start" />
                    复制
                  </Button>
                )}
              </div>
              <div className="max-h-64 overflow-y-auto overflow-x-hidden rounded-lg bg-muted/60 p-3">
                <pre className="font-mono text-xs text-foreground whitespace-pre-wrap break-all leading-relaxed">
                  {formatDetail(selectedLog?.detail)}
                </pre>
              </div>
            </div>
          </div>

          <DrawerFooter className="border-t pt-3">
            <DrawerClose
              render={
                <Button variant="outline" className="w-full">
                  关闭
                </Button>
              }
            />
          </DrawerFooter>
        </DrawerContent>
      </Drawer>
    </div>
  )
}
