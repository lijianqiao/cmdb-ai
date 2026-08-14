/** CMDB 资产新增/编辑表单对话框
 *
 * 凭据类型三态切换是这里的核心：none 不显示账号密码；static 显示账号+密码
 * （编辑时密码框留空 = 不修改，placeholder 提示"留空则不修改已设置的密码"）；
 * dynamic 只显示账号，不显示密码输入框。
 *
 * 布局：短字段两列紧凑排布；内容区可滚动，底部按钮始终可见。
 */

import { useEffect, useMemo, useState } from "react"
import { Controller, useForm, useWatch } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { toast } from "sonner"

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
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group"
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
import { fetchCmdbAssetCredential } from "@/lib/cmdb-credential-api"
import { PERMISSIONS } from "@/lib/constants"
import { ViewIcon, ViewOffSlashIcon } from "@/lib/icons"
import { usePermission } from "@/hooks/use-permission"
import type {
  CmdbAsset,
  CmdbAssetCreate,
  CmdbAssetUpdate,
  CredentialType,
  VendorName,
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

const VENDOR_ITEMS: { label: string; value: VendorName }[] = [
  { label: "通用", value: "generic" },
  { label: "思科 IOS-XE", value: "cisco_iosxe" },
  { label: "华为 VRP", value: "huawei_vrp" },
  { label: "H3C Comware", value: "hp_comware" },
  { label: "Juniper Junos", value: "juniper_junos" },
  { label: "Linux", value: "linux" },
]

const CREDENTIAL_TYPE_ITEMS: { label: string; value: CredentialType }[] = [
  { label: "无", value: "none" },
  { label: "静态密码", value: "static" },
  { label: "动态密码（仅记账号）", value: "dynamic" },
]

interface CmdbCredentialRevealDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  password: string
  assetHostname?: string
}

/** 静态凭据明文查看对话框（默认隐藏字符，可切换显示） */
export function CmdbCredentialRevealDialog({
  open,
  onOpenChange,
  password,
  assetHostname,
}: CmdbCredentialRevealDialogProps) {
  const [showPassword, setShowPassword] = useState(false)

  const handleOpenChange = (next: boolean) => {
    if (!next) {
      setShowPassword(false)
    }
    onOpenChange(next)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>查看密码</DialogTitle>
          {assetHostname ? (
            <DialogDescription>
              资产「{assetHostname}」的静态登录密码
            </DialogDescription>
          ) : null}
        </DialogHeader>
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="cmdb-credential-reveal">登录密码</FieldLabel>
            <InputGroup>
              <InputGroupInput
                id="cmdb-credential-reveal"
                type={showPassword ? "text" : "password"}
                readOnly
                value={password}
              />
              <InputGroupAddon align="inline-end">
                <InputGroupButton
                  type="button"
                  size="icon-xs"
                  aria-label={showPassword ? "隐藏密码" : "显示密码"}
                  aria-pressed={showPassword}
                  onClick={() => setShowPassword((prev) => !prev)}
                >
                  {showPassword ? <ViewOffSlashIcon /> : <ViewIcon />}
                </InputGroupButton>
              </InputGroupAddon>
            </InputGroup>
          </Field>
        </FieldGroup>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => handleOpenChange(false)}
          >
            关闭
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

interface CmdbAssetFormDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  asset?: CmdbAsset | null
  onSubmit: (data: CmdbAssetCreate | CmdbAssetUpdate) => Promise<boolean>
}

function resolveVendor(value: string | undefined): VendorName {
  const known: readonly VendorName[] = [
    "cisco_iosxe",
    "huawei_vrp",
    "hp_comware",
    "juniper_junos",
    "linux",
    "generic",
  ]
  if (value && (known as readonly string[]).includes(value)) {
    return value as VendorName
  }
  return "generic"
}

function defaultValues(asset?: CmdbAsset | null): CmdbAssetFormValues {
  return {
    asset_type: asset?.asset_type || "server",
    vendor: resolveVendor(asset?.vendor),
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
  const { hasPermission } = usePermission()
  const isEdit = !!asset
  const [credentialDialogOpen, setCredentialDialogOpen] = useState(false)
  const [revealedPassword, setRevealedPassword] = useState("")
  const [credentialLoading, setCredentialLoading] = useState(false)
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

  const credentialType = useWatch({
    control: form.control,
    name: "credential_type",
  })

  const canViewCredential =
    isEdit &&
    asset?.credential_type === "static" &&
    asset?.credential_password_set &&
    hasPermission(PERMISSIONS.CMDB_CREDENTIAL_READ)

  const handleCredentialDialogOpenChange = (open: boolean) => {
    setCredentialDialogOpen(open)
    if (!open) {
      setRevealedPassword("")
    }
  }

  const handleViewCredential = async () => {
    if (!asset) return
    setCredentialLoading(true)
    try {
      const password = await fetchCmdbAssetCredential(asset.id)
      setRevealedPassword(password)
      setCredentialDialogOpen(true)
    } catch {
      toast.error("查看密码失败")
    } finally {
      setCredentialLoading(false)
    }
  }

  const assetTypeItems = useMemo(() => {
    const current = asset?.asset_type
    if (!current || ASSET_TYPE_ITEMS.some((item) => item.value === current)) {
      return ASSET_TYPE_ITEMS
    }
    return [...ASSET_TYPE_ITEMS, { label: current, value: current }]
  }, [asset?.asset_type])

  const vendorItems = useMemo(() => {
    const current = asset?.vendor
    if (!current || VENDOR_ITEMS.some((item) => item.value === current)) {
      return VENDOR_ITEMS
    }
    return [...VENDOR_ITEMS, { label: current, value: current }]
  }, [asset?.vendor])

  const handleSubmit = async (data: CmdbAssetFormValues) => {
    const passwordChanged =
      data.credential_type === "static" && data.credential_password !== ""
    const payload: CmdbAssetCreate | CmdbAssetUpdate = {
      asset_type: data.asset_type,
      vendor: data.vendor,
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
    <>
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
                name="vendor"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="asset-vendor">厂商</FieldLabel>
                    <Select
                      items={vendorItems}
                      value={field.value}
                      onValueChange={(value) =>
                        field.onChange(value ?? "generic")
                      }
                    >
                      <SelectTrigger id="asset-vendor">
                        <SelectValue placeholder="选择厂商" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {vendorItems.map((item) => (
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
                      <div className="flex items-center gap-2">
                        <FieldLabel htmlFor="asset-credential-password">
                          登录密码
                          {isEdit && asset?.credential_password_set && (
                            <span className="ml-2 text-xs font-normal text-muted-foreground">
                              已设置
                            </span>
                          )}
                        </FieldLabel>
                        {canViewCredential && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="h-auto px-2 py-0 text-xs"
                            disabled={credentialLoading}
                            onClick={handleViewCredential}
                          >
                            {credentialLoading && (
                              <Spinner data-icon="inline-start" />
                            )}
                            查看密码
                          </Button>
                        )}
                      </div>
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
    <CmdbCredentialRevealDialog
      open={credentialDialogOpen}
      onOpenChange={handleCredentialDialogOpenChange}
      password={revealedPassword}
      assetHostname={asset?.hostname}
    />
    </>
  )
}
