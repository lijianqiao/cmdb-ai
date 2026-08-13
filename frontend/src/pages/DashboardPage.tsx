/** 仪表盘页
 *
 * 运维指标卡片优先：资产、监控、离线、待审批；账号权限指标放第二行。
 */

import { useEffect, useState } from "react"
import { useNavigate } from "react-router"
import dayjs from "dayjs"
import { toast } from "sonner"

import {
  Alert02Icon,
  AiChat01Icon,
  Database02Icon,
  FileEditIcon,
  PlusSignIcon,
  Server02Icon,
  Shield02Icon,
  UserCheck02Icon,
  UserMultipleIcon,
} from "@/lib/icons"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { PageHeader } from "@/components/layout/PageHeader"
import api from "@/lib/api"
import { usePermission } from "@/hooks/use-permission"
import { PERMISSIONS, ROUTES } from "@/lib/constants"
import type { DashboardData } from "@/types/audit"

interface StatCardItem {
  label: string
  value: number | undefined
  icon: typeof Database02Icon
}

function StatCards({
  items,
  isLoading,
  loadError,
}: {
  items: StatCardItem[]
  isLoading: boolean
  loadError: boolean
}) {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
      {items.map((stat) => {
        const Icon = stat.icon
        return (
          <Card key={stat.label}>
            <CardHeader className="flex flex-row items-center gap-4">
              <div className="flex size-12 items-center justify-center rounded-lg bg-muted text-muted-foreground [&_svg]:size-6">
                <Icon />
              </div>
              <div className="min-w-0">
                {isLoading ? (
                  <Skeleton className="h-8 w-16" />
                ) : loadError ? (
                  <CardTitle>—</CardTitle>
                ) : (
                  <CardTitle>{stat.value ?? 0}</CardTitle>
                )}
                <CardDescription>{stat.label}</CardDescription>
              </div>
            </CardHeader>
          </Card>
        )
      })}
    </div>
  )
}

export function DashboardPage() {
  const navigate = useNavigate()
  const { hasPermission } = usePermission()
  const [data, setData] = useState<DashboardData | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)

  useEffect(() => {
    const fetchData = async () => {
      setIsLoading(true)
      setLoadError(false)
      try {
        const response = await api.get("/dashboard")
        setData(response.data?.data)
      } catch {
        setData(null)
        setLoadError(true)
        toast.error("获取仪表盘数据失败")
      } finally {
        setIsLoading(false)
      }
    }
    void fetchData()
  }, [])

  const opsStats: StatCardItem[] = [
    {
      label: "CMDB 资产",
      value: data?.stats.cmdb_asset_count,
      icon: Database02Icon,
    },
    {
      label: "监控目标",
      value: data?.stats.monitor_target_count,
      icon: Server02Icon,
    },
    {
      label: "离线目标",
      value: data?.stats.monitor_down_count,
      icon: Alert02Icon,
    },
    {
      label: "待审批变更",
      value: data?.stats.pending_hitl_count,
      icon: AiChat01Icon,
    },
  ]

  const adminStats: StatCardItem[] = [
    {
      label: "设备命令策略",
      value: data?.stats.device_command_policy_count,
      icon: FileEditIcon,
    },
    {
      label: "用户总数",
      value: data?.stats.user_count,
      icon: UserMultipleIcon,
    },
    {
      label: "角色总数",
      value: data?.stats.role_count,
      icon: Shield02Icon,
    },
    {
      label: "启用用户",
      value: data?.stats.active_user_count,
      icon: UserCheck02Icon,
    },
  ]

  return (
    <div>
      <PageHeader
        title="仪表盘"
        description="运维运行概览：资产、监控探活、待审批变更与命令策略"
      />

      {loadError && !isLoading && (
        <Alert variant="destructive" className="mb-4">
          <Alert02Icon />
          <AlertTitle>加载失败</AlertTitle>
          <AlertDescription>
            仪表盘数据加载失败，请刷新页面重试。
          </AlertDescription>
        </Alert>
      )}

      <StatCards items={opsStats} isLoading={isLoading} loadError={loadError} />
      <div className="mt-4">
        <StatCards
          items={adminStats}
          isLoading={isLoading}
          loadError={loadError}
        />
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">最近操作日志</CardTitle>
            <CardDescription>最近 10 条系统操作记录</CardDescription>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <div className="flex flex-col gap-2">
                {Array.from({ length: 5 }).map((_, index) => (
                  <Skeleton key={index} className="h-8 w-full" />
                ))}
              </div>
            ) : loadError ? (
              <p className="py-6 text-center text-sm text-muted-foreground">
                无法加载操作记录
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead>用户</TableHead>
                    <TableHead>操作</TableHead>
                    <TableHead>IP</TableHead>
                    <TableHead>时间</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data?.recent_logs?.length ? (
                    data.recent_logs.map((log) => (
                      <TableRow key={log.id}>
                        <TableCell className="font-medium">
                          {log.username || "未知"}
                        </TableCell>
                        <TableCell>{log.action}</TableCell>
                        <TableCell className="text-muted-foreground">
                          {log.ip || "-"}
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {log.created_at
                            ? dayjs(log.created_at).format("MM-DD HH:mm")
                            : "-"}
                        </TableCell>
                      </TableRow>
                    ))
                  ) : (
                    <TableRow>
                      <TableCell
                        colSpan={4}
                        className="text-center text-muted-foreground"
                      >
                        暂无记录
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg">快捷操作</CardTitle>
            <CardDescription>常用运维入口</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-3">
            <Button
              onClick={() => navigate(ROUTES.OPS_ASSISTANT)}
              variant="outline"
            >
              <AiChat01Icon data-icon="inline-start" />
              运维助手
            </Button>
            {hasPermission(PERMISSIONS.CMDB_MANAGE) && (
              <Button
                onClick={() => navigate(`${ROUTES.CMDB}?create=1`)}
                variant="outline"
              >
                <PlusSignIcon data-icon="inline-start" />
                新增资产
              </Button>
            )}
            {hasPermission(PERMISSIONS.MONITOR_MANAGE) && (
              <Button
                onClick={() => navigate(`${ROUTES.MONITOR_TARGETS}?create=1`)}
                variant="outline"
              >
                <PlusSignIcon data-icon="inline-start" />
                新增监控
              </Button>
            )}
            {hasPermission(PERMISSIONS.DEVICE_COMMAND_POLICY_READ) && (
              <Button
                onClick={() => navigate(ROUTES.DEVICE_COMMAND_POLICIES)}
                variant="outline"
              >
                <FileEditIcon data-icon="inline-start" />
                设备命令策略
              </Button>
            )}
            {hasPermission(PERMISSIONS.AUDIT_READ) && (
              <Button onClick={() => navigate(ROUTES.AUDIT)} variant="outline">
                查看日志
              </Button>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
