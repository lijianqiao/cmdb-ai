/** HITL 与监控运行参数配置卡片 */

import { useEffect } from "react"
import { Controller, useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { isAxiosError } from "axios"
import { toast } from "sonner"

import { Alert02Icon } from "@/lib/icons"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import { updateOperationsSystemConfig } from "@/lib/system-config-api"
import type {
  OperationsSystemConfig,
  SystemConfigData,
} from "@/types/system-config"

import {
  operationsConfigFormSchema,
  type OperationsConfigFormValues,
} from "./systemConfigFormSchemas"

export interface OperationsConfigCardProps {
  value: OperationsSystemConfig
  onSaved: (next: SystemConfigData) => void
}

/**
 * 读取接口错误中的中文 message/detail。
 *
 * Args:
 *   error: 捕获的异常
 *   fallback: 默认文案
 *
 * Returns:
 *   可展示的错误字符串
 */
function readErrorMessage(error: unknown, fallback: string): string {
  if (!isAxiosError(error)) return fallback
  const data = error.response?.data
  if (data && typeof data === "object") {
    const message = (data as { message?: unknown }).message
    if (typeof message === "string" && message.trim()) return message
    const detail = (data as { detail?: unknown }).detail
    if (typeof detail === "string" && detail.trim()) return detail
  }
  return fallback
}

function toFormValues(value: OperationsSystemConfig): OperationsConfigFormValues {
  return {
    hitl_notify_auto_approve: value.hitl_notify_auto_approve,
    monitor_probe_timeout_seconds: value.monitor_probe_timeout_seconds,
    monitor_sweep_interval_seconds: value.monitor_sweep_interval_seconds,
    cmdb_diff_interval_seconds: value.cmdb_diff_interval_seconds,
    monitor_event_retention_days: value.monitor_event_retention_days,
  }
}

/**
 * HITL 与监控运行参数表单卡片。
 *
 * Args:
 *   value: 当前运行参数快照
 *   onSaved: 保存成功后的回调，携带最新完整配置
 */
export function OperationsConfigCard({
  value,
  onSaved,
}: OperationsConfigCardProps) {
  const form = useForm<OperationsConfigFormValues>({
    resolver: zodResolver(operationsConfigFormSchema),
    defaultValues: toFormValues(value),
  })

  useEffect(() => {
    form.reset(toFormValues(value))
  }, [value, form])

  const autoApprove = form.watch("hitl_notify_auto_approve")

  const handleSubmit = async (data: OperationsConfigFormValues) => {
    try {
      const next = await updateOperationsSystemConfig(data)
      toast.success("运行配置已保存")
      onSaved(next)
    } catch (error) {
      toast.error(readErrorMessage(error, "保存运行配置失败"))
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>运行参数</CardTitle>
        <CardDescription>
          配置 HITL 自动批准策略与监控巡检周期
        </CardDescription>
      </CardHeader>
      <form onSubmit={form.handleSubmit(handleSubmit)}>
        <CardContent>
          <FieldGroup className="gap-4">
            <Controller
              control={form.control}
              name="hitl_notify_auto_approve"
              render={({ field }) => (
                <Field>
                  <div className="flex items-center justify-between gap-4">
                    <FieldLabel htmlFor="hitl-notify-auto-approve">
                      notify 自动批准
                    </FieldLabel>
                    <Switch
                      id="hitl-notify-auto-approve"
                      checked={field.value}
                      onCheckedChange={field.onChange}
                      aria-label="notify 自动批准"
                    />
                  </div>
                  <FieldDescription>
                    开启后，notify 类型提案会跳过人工审批并立即执行；不会自动批准
                    device_query 或 device_control。
                  </FieldDescription>
                </Field>
              )}
            />

            {autoApprove ? (
              <Alert className="border-amber-500/40 bg-amber-500/10">
                <Alert02Icon />
                <AlertTitle>已开启 notify 自动批准</AlertTitle>
                <AlertDescription>
                  notify 类型提案将跳过人工审批并立即执行，请确认符合安全策略。
                </AlertDescription>
              </Alert>
            ) : null}

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <Controller
                control={form.control}
                name="monitor_probe_timeout_seconds"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="monitor-probe-timeout">
                      探测超时（秒）
                    </FieldLabel>
                    <Input
                      id="monitor-probe-timeout"
                      type="number"
                      min={1}
                      max={30}
                      aria-invalid={fieldState.invalid}
                      {...field}
                    />
                    <FieldDescription>
                      单个 TCP 连接探测允许等待的最长时间，范围为 (0, 30]
                      秒；下一轮监控探测生效。
                    </FieldDescription>
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />

              <Controller
                control={form.control}
                name="monitor_sweep_interval_seconds"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="monitor-sweep-interval">
                      巡检间隔（秒）
                    </FieldLabel>
                    <Input
                      id="monitor-sweep-interval"
                      type="number"
                      min={5}
                      max={3600}
                      aria-invalid={fieldState.invalid}
                      {...field}
                    />
                    <FieldDescription>
                      全部启用目标探测完成后，到下一轮开始前的全局等待时间，范围为
                      [5, 3600] 秒。
                    </FieldDescription>
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />

              <Controller
                control={form.control}
                name="cmdb_diff_interval_seconds"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="cmdb-diff-interval">
                      CMDB 差异巡检（秒）
                    </FieldLabel>
                    <Input
                      id="cmdb-diff-interval"
                      type="number"
                      min={60}
                      max={86400}
                      aria-invalid={fieldState.invalid}
                      {...field}
                    />
                    <FieldDescription>
                      比较监控在线 IP 与 CMDB 资产台账的周期，范围为 [60, 86400]
                      秒；只记录差异审计，不自动修改资产。
                    </FieldDescription>
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />

              <Controller
                control={form.control}
                name="monitor_event_retention_days"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="monitor-event-retention-days">
                      监控日志保留天数
                    </FieldLabel>
                    <Input
                      id="monitor-event-retention-days"
                      type="number"
                      min={1}
                      max={90}
                      aria-invalid={fieldState.invalid}
                      {...field}
                    />
                    <FieldDescription>
                      过期变化记录会被清理，每台最新一条会保留。
                    </FieldDescription>
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
            </div>
          </FieldGroup>
        </CardContent>
        <CardFooter className="justify-end border-t pt-4">
          <Button type="submit" disabled={form.formState.isSubmitting}>
            {form.formState.isSubmitting && <Spinner data-icon="inline-start" />}
            保存运行配置
          </Button>
        </CardFooter>
      </form>
    </Card>
  )
}
