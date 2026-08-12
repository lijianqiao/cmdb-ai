/** CMDB 资产新增/编辑表单对话框
 *
 * 凭据类型三态切换是这里的核心：none 不显示账号密码；static 显示账号+密码
 * （编辑时密码框留空 = 不修改，placeholder 提示"留空则不修改已设置的密码"）；
 * dynamic 只显示账号，不显示密码输入框。
 */

import { useEffect } from "react"
import { Controller, useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"

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

const CREDENTIAL_TYPE_ITEMS: { label: string; value: CredentialType }[] = [
  { label: "无", value: "none" },
  { label: "静态密码", value: "static" },
  { label: "动态密码（仅记账号）", value: "dynamic" },
]

const formSchema = z
  .object({
    asset_type: z.string().min(1, "请输入资产类型").max(50),
    hostname: z.string().min(1, "请输入主机名").max(255),
    ip_address: z.string().min(1, "请输入 IP 地址").max(45),
    location: z.string().max(200).optional().default(""),
    business_system: z.string().max(100).optional().default(""),
    subnet_cidr: z.string().max(45).optional().default(""),
    notes: z.string().max(2000).optional().default(""),
    credential_type: z.enum(["none", "static", "dynamic"]),
    credential_username: z.string().max(100).optional().default(""),
    credential_password: z.string().max(256).optional().default(""),
  })
  .superRefine((data, ctx) => {
    if (data.credential_type === "none") {
      if (data.credential_username || data.credential_password) {
        ctx.addIssue({
          code: "custom",
          path: ["credential_username"],
          message: "凭据类型为「无」时不能填写账号或密码",
        })
      }
    } else if (data.credential_type === "static") {
      if (!data.credential_username) {
        ctx.addIssue({
          code: "custom",
          path: ["credential_username"],
          message: "静态凭据必须填写账号",
        })
      }
    } else if (data.credential_type === "dynamic") {
      if (!data.credential_username) {
        ctx.addIssue({
          code: "custom",
          path: ["credential_username"],
          message: "动态凭据必须填写账号",
        })
      }
      if (data.credential_password) {
        ctx.addIssue({
          code: "custom",
          path: ["credential_password"],
          message: "动态凭据不需要也不允许填写密码",
        })
      }
    }
  })

type FormValues = z.infer<typeof formSchema>

interface CmdbAssetFormDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  asset?: CmdbAsset | null
  onSubmit: (data: CmdbAssetCreate | CmdbAssetUpdate) => Promise<boolean>
}

function defaultValues(asset?: CmdbAsset | null): FormValues {
  return {
    asset_type: asset?.asset_type ?? "",
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
  const form = useForm<FormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: defaultValues(asset),
  })

  useEffect(() => {
    form.reset(defaultValues(asset))
  }, [asset, form])

  const credentialType = form.watch("credential_type")

  const handleSubmit = async (data: FormValues) => {
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
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? "编辑资产" : "新增资产"}</DialogTitle>
          <DialogDescription>
            {isEdit ? "修改 CMDB 资产信息" : "登记一个新的 CMDB 资产"}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={form.handleSubmit(handleSubmit)}>
          <FieldGroup>
            <Controller
              control={form.control}
              name="asset_type"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid}>
                  <FieldLabel htmlFor="asset-type">资产类型</FieldLabel>
                  <Input
                    id="asset-type"
                    placeholder="如 server / switch / router"
                    {...field}
                  />
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
                  <Input id="asset-ip" {...field} />
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
                  <Input id="asset-location" {...field} />
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />
            <Controller
              control={form.control}
              name="credential_type"
              render={({ field }) => (
                <Field>
                  <FieldLabel htmlFor="asset-credential-type">
                    登录凭据类型
                  </FieldLabel>
                  <Select
                    items={CREDENTIAL_TYPE_ITEMS}
                    value={field.value}
                    onValueChange={(value) => field.onChange(value ?? "none")}
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
                  <Field data-invalid={fieldState.invalid}>
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
                <Field data-invalid={fieldState.invalid}>
                  <FieldLabel htmlFor="asset-notes">备注</FieldLabel>
                  <Textarea id="asset-notes" rows={3} {...field} />
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />
            <DialogFooter>
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
          </FieldGroup>
        </form>
      </DialogContent>
    </Dialog>
  )
}
