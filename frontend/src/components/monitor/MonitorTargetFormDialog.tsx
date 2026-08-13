/** 监控目标新增/编辑表单对话框 */

import { useEffect } from "react"
import { Controller, useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
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
import { CmdbAssetPicker } from "@/components/cmdb/CmdbAssetPicker"
import type {
  MonitorTarget,
  MonitorTargetCreate,
  MonitorTargetUpdate,
} from "@/types/monitor"

import {
  monitorTargetFormSchema,
  type MonitorTargetFormValues,
} from "./monitorTargetFormSchema"

interface MonitorTargetFormDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  target?: MonitorTarget | null
  onSubmit: (data: MonitorTargetCreate | MonitorTargetUpdate) => Promise<boolean>
}

function defaultValues(target?: MonitorTarget | null): MonitorTargetFormValues {
  return {
    ip_address: target?.ip_address ?? "",
    port: target?.port ?? 22,
    label: target?.label ?? "",
    check_interval_seconds: target?.check_interval_seconds ?? 30,
    is_active: target?.is_active ?? true,
    cmdb_asset_id: target?.cmdb_asset_id ? String(target.cmdb_asset_id) : "",
  }
}

function toPayload(
  data: MonitorTargetFormValues,
): MonitorTargetCreate | MonitorTargetUpdate {
  return {
    ip_address: data.ip_address,
    port: data.port,
    label: data.label,
    check_interval_seconds: data.check_interval_seconds,
    is_active: data.is_active,
    cmdb_asset_id: data.cmdb_asset_id ? Number(data.cmdb_asset_id) : null,
  }
}

export function MonitorTargetFormDialog({
  open,
  onOpenChange,
  target,
  onSubmit,
}: MonitorTargetFormDialogProps) {
  const isEdit = !!target
  const form = useForm<MonitorTargetFormValues>({
    resolver: zodResolver(monitorTargetFormSchema),
    defaultValues: defaultValues(target),
  })

  useEffect(() => {
    if (open) {
      form.reset(defaultValues(target))
    }
  }, [open, target, form])

  const handleSubmit = async (data: MonitorTargetFormValues) => {
    const ok = await onSubmit(toPayload(data))
    if (ok) onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[min(90dvh,40rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <DialogHeader className="shrink-0 px-6 pt-6 pb-3">
          <DialogTitle>{isEdit ? "编辑监控目标" : "新增监控目标"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? "修改探测地址、端口或启用状态"
              : "登记一个 TCP 探活目标；启用后下一轮巡检开始探测"}
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex min-h-0 flex-1 flex-col"
          onSubmit={form.handleSubmit(handleSubmit)}
        >
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-2">
            <FieldGroup>
              <Controller
                control={form.control}
                name="ip_address"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="monitor-ip">IP 地址</FieldLabel>
                    <Input
                      id="monitor-ip"
                      placeholder="10.0.0.5"
                      aria-invalid={fieldState.invalid}
                      {...field}
                    />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <Controller
                control={form.control}
                name="port"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="monitor-port">端口</FieldLabel>
                    <Input
                      id="monitor-port"
                      type="number"
                      min={1}
                      max={65535}
                      aria-invalid={fieldState.invalid}
                      {...field}
                    />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <Controller
                control={form.control}
                name="label"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="monitor-label">标签</FieldLabel>
                    <Input
                      id="monitor-label"
                      placeholder="例如：核心交换机 SSH"
                      aria-invalid={fieldState.invalid}
                      {...field}
                    />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <Controller
                control={form.control}
                name="check_interval_seconds"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="monitor-interval">
                      巡检间隔（秒）
                    </FieldLabel>
                    <Input
                      id="monitor-interval"
                      type="number"
                      min={5}
                      max={3600}
                      aria-invalid={fieldState.invalid}
                      {...field}
                    />
                    <FieldDescription>
                      当前后台按系统配置的全局间隔巡检；该字段会一并保存。
                    </FieldDescription>
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <Controller
                control={form.control}
                name="cmdb_asset_id"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="monitor-cmdb-asset">
                      关联 CMDB 资产
                    </FieldLabel>
                    <CmdbAssetPicker
                      id="monitor-cmdb-asset"
                      value={field.value ? Number(field.value) : null}
                      onChange={(assetId) =>
                        field.onChange(assetId ? String(assetId) : "")
                      }
                      allowClear
                      invalid={fieldState.invalid}
                    />
                    <FieldDescription>
                      搜索主机名或 IP 后选择；可留空，用于尚未入库的临时探测地址。
                    </FieldDescription>
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <Controller
                control={form.control}
                name="is_active"
                render={({ field }) => (
                  <Field>
                    <div className="flex items-center justify-between gap-4">
                      <FieldLabel htmlFor="monitor-is-active">启用探测</FieldLabel>
                      <Switch
                        id="monitor-is-active"
                        checked={field.value}
                        onCheckedChange={field.onChange}
                        aria-label="启用探测"
                      />
                    </div>
                    <FieldDescription>
                      关闭后后台巡检会跳过该目标。
                    </FieldDescription>
                  </Field>
                )}
              />
            </FieldGroup>
          </div>
          <DialogFooter className="shrink-0 border-t px-6 py-4">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={form.formState.isSubmitting}
            >
              取消
            </Button>
            <Button type="submit" disabled={form.formState.isSubmitting}>
              {form.formState.isSubmitting && (
                <Spinner data-icon="inline-start" />
              )}
              确定
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
