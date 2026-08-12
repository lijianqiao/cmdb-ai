/** CMDB 资产新增/编辑表单对话框
 *
 * 凭据类型三态切换是这里的核心：none 不显示账号密码；static 显示账号+密码
 * （编辑时密码框留空 = 不修改，placeholder 提示"留空则不修改已设置的密码"）；
 * dynamic 只显示账号，不显示密码输入框。
 *
 * 布局：短字段两列紧凑排布；内容区可滚动，底部按钮始终可见。
 */

import { useEffect, useMemo } from "react"
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
  FieldError,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import type {
  CmdbAsset,
  CmdbAssetCreate,
  CmdbAssetUpdate,
  CredentialType,
} from "@/types/cmdb"

import {
  clearedCredentialFields,
  createFormSchema,
  type CmdbAssetFormValues,
} from "./cmdbAssetFormSchema"

const ASSET_TYPE_ITEMS: { label: string; value: string }[] = [
  { label: "服务器", value: "server" },
  { label: "交换机", value: "switch" },
  { label: "路由器", value: "router" },
  { label: "防火墙", value: "firewall" },
  { label: "负载均衡", value: "load_balancer" },
  { label: "存储", value: "storage" },
  { label: "其他", value: "other" },
]

const CREDENTIAL_TYPE_ITEMS: { label: string; value: CredentialType }[] = [
  { label: "无", value: "none" },
  { label: "静态密码", value: "static" },
  { label: "动态密码（仅记账号）", value: "dynamic" },
]

interface CmdbAssetFormDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  asset?: CmdbAsset | null
  onSubmit: (data: CmdbAssetCreate | CmdbAssetUpdate) => Promise<boolean>
}

function defaultValues(asset?: CmdbAsset | null): CmdbAssetFormValues {
  return {
    asset_type: asset?.asset_type || "server",
    hostname: asset?.hostname ?? "",
    ip_address: asset?.ip_address ?? "",
    location: asset?.location ?? "",
    business_system: asset?.business_system ?? "",
    subnet_cidr: asset?.subnet_cidr ?? "",
    notes: asset?.notes ?? "",
    credential_type: asset?.credential_type ?? "none",
    credential_username: asset?.credential_username ?? "",
    credential_password: "",
  }
}

export function CmdbAssetFormDialog({
  open,
  onOpenChange,
  asset,
  onSubmit,
}: CmdbAssetFormDialogProps) {
  const isEdit = !!asset
  const form = useForm<CmdbAssetFormValues>({
    resolver: (data, context, options) =>
      zodResolver(createFormSchema(asset?.credential_type ?? null))(
        data,
        context,
        options
      ),
    defaultValues: defaultValues(asset),
  })

  useEffect(() => {
    if (open) {
      form.reset(defaultValues(asset))
    }
  }, [open, asset, form])

  const credentialType = form.watch("credential_type")

  const assetTypeItems = useMemo(() => {
    const current = asset?.asset_type
    if (!current || ASSET_TYPE_ITEMS.some((item) => item.value === current)) {
      return ASSET_TYPE_ITEMS
    }
    return [...ASSET_TYPE_ITEMS, { label: current, value: current }]
  }, [asset?.asset_type])

  const handleSubmit = async (data: CmdbAssetFormValues) => {
    const passwordChanged =
      data.credential_type === "static" && data.credential_password !== ""
    const payload: CmdbAssetCreate | CmdbAssetUpdate = {
      asset_type: data.asset_type,
      hostname: data.hostname,
      ip_address: data.ip_address,
      location: data.location,
      business_system: data.business_system,
      subnet_cidr: data.subnet_cidr,
      notes: data.notes,
      credential_type: data.credential_type,
      credential_username:
        data.credential_type === "none" ? "" : data.credential_username,
      // 编辑且未修改密码时，不把 credential_password 传出去（undefined 会被
      // JSON.stringify 丢弃这个键），后端据此保留原有密文不变。
      ...(data.credential_type === "static" && (passwordChanged || !isEdit)
        ? { credential_password: data.credential_password }
        : data.credential_type === "dynamic"
          ? { credential_password: null }
          : {}),
    }
    const ok = await onSubmit(payload)
    if (ok) onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[min(90dvh,40rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <DialogHeader className="shrink-0 px-6 pt-6 pb-3">
          <DialogTitle>{isEdit ? "编辑资产" : "新增资产"}</DialogTitle>
          <DialogDescription>
            {isEdit ? "修改 CMDB 资产信息" : "登记一个新的 CMDB 资产"}
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex min-h-0 flex-1 flex-col"
          onSubmit={form.handleSubmit(handleSubmit)}
        >
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-1">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Controller
                control={form.control}
                name="asset_type"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="asset-type">资产类型</FieldLabel>
                    <Select
                      items={assetTypeItems}
                      value={field.value}
                      onValueChange={(value) =>
                        field.onChange(value ?? "server")
                      }
                    >
                      <SelectTrigger id="asset-type">
                        <SelectValue placeholder="选择类型" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {assetTypeItems.map((item) => (
                            <SelectItem key={item.value} value={item.value}>
                              {item.label}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <Controller
                control={form.control}
                name="hostname"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="asset-hostname">主机名</FieldLabel>
                    <Input id="asset-hostname" {...field} />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <Controller
                control={form.control}
                name="ip_address"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="asset-ip">IP 地址</FieldLabel>
                    <Input id="asset-ip" placeholder="如 10.0.0.1" {...field} />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <Controller
                control={form.control}
                name="subnet_cidr"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="asset-subnet">子网 CIDR</FieldLabel>
                    <Input
                      id="asset-subnet"
                      placeholder="如 10.0.0.0/24"
                      {...field}
                    />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <Controller
                control={form.control}
                name="business_system"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="asset-business">业务系统</FieldLabel>
                    <Input id="asset-business" {...field} />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <Controller
                control={form.control}
                name="location"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="asset-location">位置</FieldLabel>
                    <Input
                      id="asset-location"
                      placeholder="机房 / 机柜"
                      {...field}
                    />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <Controller
                control={form.control}
                name="credential_type"
                render={({ field }) => (
                  <Field
                    className={
                      credentialType === "none" ? "sm:col-span-2" : undefined
                    }
                  >
                    <FieldLabel htmlFor="asset-credential-type">
                      登录凭据类型
                    </FieldLabel>
                    <Select
                      items={CREDENTIAL_TYPE_ITEMS}
                      value={field.value}
                      onValueChange={(value) => {
                        field.onChange(value ?? "none")
                        const cleared = clearedCredentialFields()
                        form.setValue(
                          "credential_username",
                          cleared.credential_username
                        )
                        form.setValue(
                          "credential_password",
                          cleared.credential_password
                        )
                      }}
                    >
                      <SelectTrigger id="asset-credential-type">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {CREDENTIAL_TYPE_ITEMS.map((item) => (
                            <SelectItem key={item.value} value={item.value}>
                              {item.label}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  </Field>
                )}
              />
              {credentialType !== "none" && (
                <Controller
                  control={form.control}
                  name="credential_username"
                  render={({ field, fieldState }) => (
                    <Field data-invalid={fieldState.invalid}>
                      <FieldLabel htmlFor="asset-credential-username">
                        登录账号
                      </FieldLabel>
                      <Input
                        id="asset-credential-username"
                        autoComplete="off"
                        {...field}
                      />
                      <FieldError errors={[fieldState.error]} />
                    </Field>
                  )}
                />
              )}
              {credentialType === "static" && (
                <Controller
                  control={form.control}
                  name="credential_password"
                  render={({ field, fieldState }) => (
                    <Field
                      className="sm:col-span-2"
                      data-invalid={fieldState.invalid}
                    >
                      <FieldLabel htmlFor="asset-credential-password">
                        登录密码
                        {isEdit && asset?.credential_password_set && (
                          <span className="ml-2 text-xs font-normal text-muted-foreground">
                            已设置
                          </span>
                        )}
                      </FieldLabel>
                      <Input
                        id="asset-credential-password"
                        type="password"
                        autoComplete="new-password"
                        placeholder={
                          isEdit ? "留空则不修改已设置的密码" : "请输入密码"
                        }
                        {...field}
                      />
                      <FieldError errors={[fieldState.error]} />
                    </Field>
                  )}
                />
              )}
              <Controller
                control={form.control}
                name="notes"
                render={({ field, fieldState }) => (
                  <Field className="sm:col-span-2" data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="asset-notes">备注</FieldLabel>
                    <Textarea id="asset-notes" rows={2} {...field} />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
            </div>
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
