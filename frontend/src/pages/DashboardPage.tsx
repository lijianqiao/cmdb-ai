/** 仪表盘页
 *
 * 运维指标卡片优先：资产、监控、离线、待审批；基础配置指标作为次级状态条。
 * 下方分栏展示最近系统操作日志与快捷运维通道。
 */

import { useEffect, useState } from "react"
import { useNavigate } from "react-router"
import dayjs from "dayjs"
import { toast } from "sonner"

import {
  Alert02Icon,
  AiChat01Icon,
  AuditIcon,
  Book02Icon,
  ChevronRightIcon,
  Database02Icon,
  FileEditIcon,
  PlusSignIcon,
  Server02Icon,
  Shield02Icon,
  UserCheck02Icon,
  UserMultipleIcon,
} from "@/lib/icons"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
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
import { cn } from "@/lib/utils"

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
  update_profile: "更新资料",
  change_password: "修改密码",
  reset_password: "重置密码",
  update_llm_system_config: "更新模型配置",
  update_operations_system_config: "更新运行配置",
  bootstrap_system_configs: "初始化运行配置",
  view_cmdb_credential: "查看资产凭据",
  update_session_approval_mode: "变更会话审批模式",
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

  const downCount = data?.stats.monitor_down_count ?? 0
  const pendingCount = data?.stats.pending_hitl_count ?? 0

  const heroMetrics = [
    {
      label: "CMDB 设备资产",
      value: data?.stats.cmdb_asset_count,
      subText: "网络与计算设备台账",
      icon: Database02Icon,
      badge: "资产台账",
      badgeVariant: "secondary" as const,
      onClick: () => navigate(ROUTES.CMDB),
    },
    {
      label: "探活监控目标",
      value: data?.stats.monitor_target_count,
      subText: "周期在线巡检中",
      icon: Server02Icon,
      badge: "在线探测",
      badgeVariant: "secondary" as const,
      onClick: () => navigate(ROUTES.MONITOR_TARGETS),
    },
    {
      label: "离线探活告警",
      value: data?.stats.monitor_down_count,
      subText: downCount > 0 ? "需关注异常目标" : "全部目标连接正常",
      icon: Alert02Icon,
      badge: downCount > 0 ? "异常需排查" : "运行正常",
      badgeVariant: (downCount > 0 ? "destructive" : "outline") as "destructive" | "outline",
      onClick: () => navigate(ROUTES.MONITOR_LOGS),
    },
    {
      label: "待审批变更",
      value: data?.stats.pending_hitl_count,
      subText: pendingCount > 0 ? "等待人工确认" : "当前无待审批任务",
      icon: AiChat01Icon,
      badge: pendingCount > 0 ? "待处理" : "无待办",
      badgeVariant: (pendingCount > 0 ? "default" : "outline") as "default" | "outline",
      onClick: () => navigate(ROUTES.OPS_ASSISTANT),
    },
  ]

  const subMetrics = [
    {
      label: "设备命令策略",
      value: data?.stats.device_command_policy_count ?? 0,
      unit: "条已生效",
      icon: FileEditIcon,
      onClick: () => navigate(ROUTES.DEVICE_COMMAND_POLICIES),
    },
    {
      label: "系统用户总数",
      value: data?.stats.user_count ?? 0,
      unit: "位已注册",
      icon: UserMultipleIcon,
      onClick: () => navigate(ROUTES.USERS),
    },
    {
      label: "正常启用用户",
      value: data?.stats.active_user_count ?? 0,
      unit: "位可用",
      icon: UserCheck02Icon,
      onClick: () => navigate(ROUTES.USERS),
    },
    {
      label: "权限角色配置",
      value: data?.stats.role_count ?? 0,
      unit: "个角色组",
      icon: Shield02Icon,
      onClick: () => navigate(ROUTES.ROLES),
    },
  ]

  const quickActions = [
    {
      title: "运维助手智能对话",
      desc: "网络设备查询、配置巡检与排障",
      icon: AiChat01Icon,
      route: ROUTES.OPS_ASSISTANT,
      perm: PERMISSIONS.AGENT_USE,
      tag: "AI 对话",
    },
    {
      title: "录入 CMDB 设备资产",
      desc: "新增交换机、路由器或服务器",
      icon: PlusSignIcon,
      route: `${ROUTES.CMDB}?create=1`,
      perm: PERMISSIONS.CMDB_MANAGE,
    },
    {
      title: "配置探活监控目标",
      desc: "添加 TCP 端口与主机探活任务",
      icon: Server02Icon,
      route: `${ROUTES.MONITOR_TARGETS}?create=1`,
      perm: PERMISSIONS.MONITOR_MANAGE,
    },
    {
      title: "设备命令策略管理",
      desc: "配置命令免审批白名单与黑名单",
      icon: FileEditIcon,
      route: ROUTES.DEVICE_COMMAND_POLICIES,
      perm: PERMISSIONS.DEVICE_COMMAND_POLICY_READ,
    },
    {
      title: "运维知识库与文档",
      desc: "SOP 文档上传与 AI 分类管理",
      icon: Book02Icon,
      route: ROUTES.KNOWLEDGE,
      perm: PERMISSIONS.KNOWLEDGE_READ,
    },
    {
      title: "系统操作审计日志",
      desc: "追溯管理员操作与系统配置变更",
      icon: AuditIcon,
      route: ROUTES.AUDIT,
      perm: PERMISSIONS.AUDIT_READ,
    },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="仪表盘"
        description="运维系统运行总览：核心设备资产、探活状态、待审批变更与常用入口"
      />

      {loadError && !isLoading && (
        <Alert variant="destructive">
          <Alert02Icon />
          <AlertTitle>加载失败</AlertTitle>
          <AlertDescription>
            仪表盘数据加载失败，请刷新页面重试。
          </AlertDescription>
        </Alert>
      )}

      {/* 1. 核心四大运维指标卡片 */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {heroMetrics.map((item) => {
          const Icon = item.icon
          return (
            <Card
              key={item.label}
              className="group relative cursor-pointer overflow-hidden border bg-card transition-all hover:border-foreground/20 hover:shadow-sm"
              onClick={item.onClick}
            >
              <CardHeader className="pb-2">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-muted-foreground">
                    {item.label}
                  </span>
                  <div className="flex size-9 items-center justify-center rounded-lg bg-muted/60 text-muted-foreground transition-colors group-hover:bg-primary/10 group-hover:text-primary [&_svg]:size-4.5">
                    <Icon />
                  </div>
                </div>
              </CardHeader>
              <CardContent className="flex flex-col gap-2 pt-0">
                {isLoading ? (
                  <Skeleton className="h-9 w-20" />
                ) : loadError ? (
                  <span className="text-2xl font-bold">—</span>
                ) : (
                  <span className="text-3xl font-bold tracking-tight text-foreground">
                    {item.value ?? 0}
                  </span>
                )}
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span>{item.subText}</span>
                  <Badge variant={item.badgeVariant} className="px-1.5 py-0 text-[11px]">
                    {item.badge}
                  </Badge>
                </div>
              </CardContent>
            </Card>
          )
        })}
      </div>

      {/* 2. 基础配置与用户指标轻量条 */}
      <div className="rounded-xl border bg-card/60 p-4">
        <div className="grid grid-cols-2 gap-4 divide-y divide-border/60 sm:divide-y-0 sm:divide-x sm:grid-cols-4">
          {subMetrics.map((sub, idx) => {
            const Icon = sub.icon
            return (
              <div
                key={sub.label}
                className={cn(
                  "flex cursor-pointer items-center gap-3 transition-opacity hover:opacity-80",
                  idx > 0 && "sm:pl-4",
                  idx >= 2 && "pt-3 sm:pt-0",
                )}
                onClick={sub.onClick}
              >
                <div className="flex size-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground [&_svg]:size-4">
                  <Icon />
                </div>
                <div className="flex min-w-0 flex-col">
                  <span className="text-xs text-muted-foreground">{sub.label}</span>
                  <span className="text-sm font-semibold text-foreground">
                    {isLoading ? "…" : sub.value}{" "}
                    <span className="text-xs font-normal text-muted-foreground">
                      {sub.unit}
                    </span>
                  </span>
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* 3. 底部双列：最近操作日志 + 快捷操作 */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        {/* 左侧：最近操作记录 */}
        <Card className="lg:col-span-7">
          <CardHeader className="flex flex-row items-center justify-between border-b pb-3">
            <div>
              <CardTitle className="text-base font-semibold">
                最近操作记录
              </CardTitle>
              <CardDescription className="text-xs">
                展示系统最近 10 条操作与变更事件
              </CardDescription>
            </div>
            {hasPermission(PERMISSIONS.AUDIT_READ) && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-8 text-xs text-muted-foreground"
                onClick={() => navigate(ROUTES.AUDIT)}
              >
                查看全部
                <ChevronRightIcon className="size-3.5" />
              </Button>
            )}
          </CardHeader>
          <CardContent className="p-0">
            {isLoading ? (
              <div className="flex flex-col gap-2 p-4">
                {Array.from({ length: 5 }).map((_, index) => (
                  <Skeleton key={index} className="h-9 w-full rounded-md" />
                ))}
              </div>
            ) : loadError ? (
              <p className="py-8 text-center text-sm text-muted-foreground">
                无法加载操作记录
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead className="w-24">用户</TableHead>
                    <TableHead className="w-32">操作行为</TableHead>
                    <TableHead>IP 地址</TableHead>
                    <TableHead className="text-right">发生时间</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data?.recent_logs?.length ? (
                    data.recent_logs.map((log) => (
                      <TableRow key={log.id} className="text-xs">
                        <TableCell className="font-medium text-foreground">
                          {log.username || "未知"}
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline" className="font-normal text-[11px]">
                            {ACTION_LABELS[log.action] ?? log.action}
                          </Badge>
                        </TableCell>
                        <TableCell className="font-mono text-muted-foreground">
                          {log.ip || "-"}
                        </TableCell>
                        <TableCell className="text-right text-muted-foreground">
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
                        className="py-8 text-center text-muted-foreground"
                      >
                        暂无操作记录
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        {/* 右侧：快捷运维通道 */}
        <Card className="lg:col-span-5">
          <CardHeader className="border-b pb-3">
            <CardTitle className="text-base font-semibold">
              快捷运维通道
            </CardTitle>
            <CardDescription className="text-xs">
              常用运维模块一键直达
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-2.5 p-4 sm:grid-cols-2 lg:grid-cols-1">
            {quickActions.map((action) => {
              if (action.perm && !hasPermission(action.perm)) return null
              const Icon = action.icon
              return (
                <div
                  key={action.title}
                  className="group flex cursor-pointer items-center justify-between rounded-lg border border-border/70 bg-card p-3 transition-all hover:border-foreground/20 hover:bg-muted/40"
                  onClick={() => navigate(action.route)}
                >
                  <div className="flex items-center gap-3">
                    <div className="flex size-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground transition-colors group-hover:bg-primary/10 group-hover:text-primary [&_svg]:size-4">
                      <Icon />
                    </div>
                    <div className="flex flex-col">
                      <div className="flex items-center gap-1.5">
                        <span className="text-xs font-medium text-foreground">
                          {action.title}
                        </span>
                        {action.tag && (
                          <Badge variant="secondary" className="px-1 py-0 text-[10px]">
                            {action.tag}
                          </Badge>
                        )}
                      </div>
                      <span className="text-[11px] text-muted-foreground">
                        {action.desc}
                      </span>
                    </div>
                  </div>
                  <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-foreground" />
                </div>
              )
            })}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

