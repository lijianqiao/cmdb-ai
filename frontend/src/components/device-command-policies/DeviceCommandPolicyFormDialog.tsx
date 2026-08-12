/** 设备命令策略新增/编辑表单对话框
 *
 * 创建时选择作用域、目标与目录命令名；编辑时仅可修改 decision/note，
 * 与后端 DeviceCommandPolicyUpdate 收窄一致。命令名只能从 DEVICE_COMMAND_NAMES
 * 下拉选择，不接受自由输入的原始命令字符串。
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
  FieldDescription,
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
import {
  DEVICE_COMMAND_NAMES,
  type DeviceCommandPolicy,
  type DeviceCommandPolicyCreate,
  type DeviceCommandPolicyUpdate,
  type PolicyDecision,
  type PolicyScope,
} from "@/types/device-command-policy"

const ASSET_TYPE_ITEMS: { label: string; value: string }[] = [
  { label: "服务器", value: "server" },
  { label: "交换机", value: "switch" },
  { label: "路由器", value: "router" },
  { label: "防火墙", value: "firewall" },
  { label: "负载均衡", value: "load_balancer" },
  { label: "存储", value: "storage" },
  { label: "其他", value: "other" },
]

const SCOPE_ITEMS: { label: string; value: PolicyScope }[] = [
  { label: "设备类型级别", value: "asset_type" },
  { label: "单台设备", value: "asset" },
]

const DECISION_ITEMS: { label: string; value: PolicyDecision }[] = [
  { label: "白名单", value: "whitelist" },
  { label: "黑名单", value: "blacklist" },
]

const COMMAND_ITEMS = DEVICE_COMMAND_NAMES.map((name) => ({
  label: name,
  value: name,
}))

const createSchema = z
  .object({
    scope: z.enum(["asset_type", "asset"]),
    asset_type: z.string().optional(),
    asset_id: z.string().optional(),
    command_name: z.string().min(1, "请选择命令"),
    decision: z.enum(["whitelist", "blacklist"]),
    note: z.string().max(500).optional().default(""),
  })
  .superRefine((data, ctx) => {
    if (data.scope === "asset_type") {
      if (!data.asset_type) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "请选择设备类型",
          path: ["asset_type"],
        })
      }
    } else if (!data.asset_id || !/^\d+$/.test(data.asset_id)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "请输入有效的资产 ID",
        path: ["asset_id"],
      })
    }
  })

const editSchema = z.object({
  decision: z.enum(["whitelist", "blacklist"]),
  note: z.string().max(500).optional().default(""),
})

type CreateFormData = z.infer<typeof createSchema>
type EditFormData = z.infer<typeof editSchema>

interface DeviceCommandPolicyFormDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  policy?: DeviceCommandPolicy | null
  onSubmit: (
    data: DeviceCommandPolicyCreate | DeviceCommandPolicyUpdate
  ) => Promise<boolean>
}

function formatPolicyTarget(policy: DeviceCommandPolicy): string {
  if (policy.scope === "asset_type") {
    const label =
      ASSET_TYPE_ITEMS.find((item) => item.value === policy.asset_type)?.label ??
      policy.asset_type
    return `类型：${label ?? policy.asset_type}`
  }
  return `资产 #${policy.asset_id}`
}

export function DeviceCommandPolicyFormDialog({
  open,
  onOpenChange,
  policy,
  onSubmit,
}: DeviceCommandPolicyFormDialogProps) {
  const isEdit = !!policy

  const createForm = useForm<CreateFormData>({
    resolver: zodResolver(createSchema),
    defaultValues: {
      scope: "asset_type",
      asset_type: "server",
      asset_id: "",
      command_name: DEVICE_COMMAND_NAMES[0],
      decision: "whitelist",
      note: "",
    },
  })

  const editForm = useForm<EditFormData>({
    resolver: zodResolver(editSchema),
    defaultValues: { decision: "whitelist", note: "" },
  })

  const scope = createForm.watch("scope")

  useEffect(() => {
    if (!open) return
    if (policy) {
      editForm.reset({
        decision: policy.decision,
        note: policy.note ?? "",
      })
    } else {
      createForm.reset({
        scope: "asset_type",
        asset_type: "server",
        asset_id: "",
        command_name: DEVICE_COMMAND_NAMES[0],
        decision: "whitelist",
        note: "",
      })
    }
  }, [open, policy, createForm, editForm])

  const handleCreateSubmit = async (data: CreateFormData) => {
    const payload: DeviceCommandPolicyCreate = {
      scope: data.scope,
      command_name: data.command_name,
      decision: data.decision,
      note: data.note,
    }
    if (data.scope === "asset_type") {
      payload.asset_type = data.asset_type
    } else {
      payload.asset_id = Number(data.asset_id)
    }
    const ok = await onSubmit(payload)
    if (ok) onOpenChange(false)
  }

  const handleEditSubmit = async (data: EditFormData) => {
    const ok = await onSubmit(data)
    if (ok) onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{isEdit ? "编辑策略" : "新增策略"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? "仅可修改决定与备注，作用域与命令名创建后不可变"
              : "为设备类型或单台设备配置命令白/黑名单"}
          </DialogDescription>
        </DialogHeader>

        {isEdit && policy ? (
          <form onSubmit={editForm.handleSubmit(handleEditSubmit)}>
            <FieldGroup>
              <Field>
                <FieldLabel>目标</FieldLabel>
                <Input value={formatPolicyTarget(policy)} disabled />
              </Field>
              <Field>
                <FieldLabel>命令名</FieldLabel>
                <Input
                  value={policy.command_name}
                  disabled
                  className="font-mono"
                />
              </Field>
              <Controller
                control={editForm.control}
                name="decision"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="policy-decision">决定</FieldLabel>
                    <Select
                      items={DECISION_ITEMS}
                      value={field.value}
                      onValueChange={(value) =>
                        field.onChange(value as PolicyDecision)
                      }
                    >
                      <SelectTrigger
                        id="policy-decision"
                        aria-invalid={fieldState.invalid}
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {DECISION_ITEMS.map((item) => (
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
                control={editForm.control}
                name="note"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="policy-note">备注</FieldLabel>
                    <Textarea
                      id="policy-note"
                      placeholder="策略说明（选填）"
                      className="resize-none"
                      aria-invalid={fieldState.invalid}
                      {...field}
                    />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => onOpenChange(false)}
                  disabled={editForm.formState.isSubmitting}
                >
                  取消
                </Button>
                <Button type="submit" disabled={editForm.formState.isSubmitting}>
                  {editForm.formState.isSubmitting && (
                    <Spinner data-icon="inline-start" />
                  )}
                  确定
                </Button>
              </DialogFooter>
            </FieldGroup>
          </form>
        ) : (
          <form onSubmit={createForm.handleSubmit(handleCreateSubmit)}>
            <FieldGroup>
              <Controller
                control={createForm.control}
                name="scope"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="policy-scope">作用域</FieldLabel>
                    <Select
                      items={SCOPE_ITEMS}
                      value={field.value}
                      onValueChange={(value) =>
                        field.onChange(value as PolicyScope)
                      }
                    >
                      <SelectTrigger
                        id="policy-scope"
                        aria-invalid={fieldState.invalid}
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {SCOPE_ITEMS.map((item) => (
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

              {scope === "asset_type" ? (
                <Controller
                  control={createForm.control}
                  name="asset_type"
                  render={({ field, fieldState }) => (
                    <Field data-invalid={fieldState.invalid}>
                      <FieldLabel htmlFor="policy-asset-type">
                        设备类型
                      </FieldLabel>
                      <Select
                        items={ASSET_TYPE_ITEMS}
                        value={field.value}
                        onValueChange={field.onChange}
                      >
                        <SelectTrigger
                          id="policy-asset-type"
                          aria-invalid={fieldState.invalid}
                        >
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectGroup>
                            {ASSET_TYPE_ITEMS.map((item) => (
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
              ) : (
                <Controller
                  control={createForm.control}
                  name="asset_id"
                  render={({ field, fieldState }) => (
                    <Field data-invalid={fieldState.invalid}>
                      <FieldLabel htmlFor="policy-asset-id">资产 ID</FieldLabel>
                      <Input
                        id="policy-asset-id"
                        type="number"
                        min={1}
                        placeholder="输入 CMDB 资产 ID"
                        aria-invalid={fieldState.invalid}
                        {...field}
                      />
                      <FieldDescription>
                        v1 暂不支持资产搜索选择器，请直接输入资产 ID。
                      </FieldDescription>
                      <FieldError errors={[fieldState.error]} />
                    </Field>
                  )}
                />
              )}

              <Controller
                control={createForm.control}
                name="command_name"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="policy-command">命令名</FieldLabel>
                    <Select
                      items={COMMAND_ITEMS}
                      value={field.value}
                      onValueChange={field.onChange}
                    >
                      <SelectTrigger
                        id="policy-command"
                        className="font-mono"
                        aria-invalid={fieldState.invalid}
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {COMMAND_ITEMS.map((item) => (
                            <SelectItem key={item.value} value={item.value}>
                              {item.label}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                    <FieldDescription>
                      仅可选择目录中的命令名，不能输入原始命令字符串。
                    </FieldDescription>
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />

              <Controller
                control={createForm.control}
                name="decision"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="policy-create-decision">决定</FieldLabel>
                    <Select
                      items={DECISION_ITEMS}
                      value={field.value}
                      onValueChange={(value) =>
                        field.onChange(value as PolicyDecision)
                      }
                    >
                      <SelectTrigger
                        id="policy-create-decision"
                        aria-invalid={fieldState.invalid}
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {DECISION_ITEMS.map((item) => (
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
                control={createForm.control}
                name="note"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="policy-create-note">备注</FieldLabel>
                    <Textarea
                      id="policy-create-note"
                      placeholder="策略说明（选填）"
                      className="resize-none"
                      aria-invalid={fieldState.invalid}
                      {...field}
                    />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />

              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => onOpenChange(false)}
                  disabled={createForm.formState.isSubmitting}
                >
                  取消
                </Button>
                <Button
                  type="submit"
                  disabled={createForm.formState.isSubmitting}
                >
                  {createForm.formState.isSubmitting && (
                    <Spinner data-icon="inline-start" />
                  )}
                  确定
                </Button>
              </DialogFooter>
            </FieldGroup>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
