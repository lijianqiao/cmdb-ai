/** LLM 与 Embedding 模型配置卡片 */

import { useEffect } from "react"
import {
  Controller,
  useForm,
  useWatch,
  type UseFormReturn,
} from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { isAxiosError } from "axios"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { updateLlmSystemConfig } from "@/lib/system-config-api"
import {
  CHAT_TIER_LABELS,
  CHAT_TIERS,
  type ChatTier,
  type ChatTierConfig,
  type ConfigValueSource,
  type LlmSystemConfig,
  type SystemConfigData,
} from "@/types/system-config"

import {
  buildLlmUpdatePayload,
  llmConfigFormSchema,
  type LlmConfigFormValues,
} from "./systemConfigFormSchemas"

export interface LlmConfigCardProps {
  value: LlmSystemConfig
  onSaved: (next: SystemConfigData) => void
}

const TIER_HINTS: Record<ChatTier, string> = {
  fast: "摘要、文档分类、设备回显压缩",
  balanced: "日常对话与普通工具调用（其它两档未配置时的回退目标）",
  strong: "只读复核等关键判断",
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

/**
 * 将密钥来源枚举映射为中文标签。
 *
 * Args:
 *   source: 配置来源
 *
 * Returns:
 *   展示用中文文案
 */
function sourceLabel(source: ConfigValueSource): string {
  switch (source) {
    case "database":
      return "数据库"
    case "environment":
      return "环境变量"
    default:
      return "未设置"
  }
}

/**
 * 把一档的有效配置转成表单初值。
 *
 * 未配置的档回显成**空**而不是回退来源的值：把平衡档的地址填进便宜档的输入框，
 * 用户一按保存就等于把便宜档真配成了平衡档，回退状态就此消失。
 */
function toTierFormValues(tier: ChatTierConfig): LlmConfigFormValues["chat_fast"] {
  const configured = tier.configured
  return {
    base_url: configured ? tier.base_url : "",
    api_key: "",
    clear_api_key: false,
    model: configured ? tier.model : "",
    input_cost_per_million_usd: configured ? tier.input_cost_per_million_usd : 0,
    output_cost_per_million_usd: configured ? tier.output_cost_per_million_usd : 0,
  }
}

function toFormValues(value: LlmSystemConfig): LlmConfigFormValues {
  return {
    chat_fast: toTierFormValues(value.chat_fast),
    chat_balanced: toTierFormValues(value.chat_balanced),
    chat_strong: toTierFormValues(value.chat_strong),
    embedding_base_url: value.embedding_base_url,
    embedding_api_key: "",
    clear_embedding_api_key: false,
    embedding_model: value.embedding_model,
  }
}

interface ChatTierFieldsProps {
  form: UseFormReturn<LlmConfigFormValues>
  tier: ChatTier
  value: ChatTierConfig
}

/**
 * 一档 chat 的完整配置区：连接信息、双向单价与 API Key。
 *
 * 未配置的档在标题旁挂「未配置 · 回退到平衡档」徽标——没有这个提示，
 * 用户会以为便宜档在生效、实际上钱是按平衡档在花。
 */
function ChatTierFields({ form, tier, value }: ChatTierFieldsProps) {
  const prefix = `chat_${tier}` as const
  const clearApiKey = useWatch({
    control: form.control,
    name: `${prefix}.clear_api_key`,
  })

  return (
    <div className="rounded-xl border bg-muted/20 p-4 transition-colors">
      <div className="mb-4 flex flex-col gap-1 border-b border-border/60 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-base font-semibold text-foreground">
              {CHAT_TIER_LABELS[tier]}
            </span>
            {tier !== "balanced" && !value.configured ? (
              <Badge variant="outline" className="text-xs font-normal text-muted-foreground">
                未配置 · 回退到平衡档
              </Badge>
            ) : tier === "balanced" ? (
              <Badge variant="secondary" className="text-xs font-normal">
                主力对话档
              </Badge>
            ) : (
              <Badge variant="secondary" className="text-xs font-normal">
                独立配置已生效
              </Badge>
            )}
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">{TIER_HINTS[tier]}</p>
        </div>
      </div>
      <FieldGroup className="gap-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Controller
            control={form.control}
            name={`${prefix}.base_url`}
            render={({ field, fieldState }) => (
              <Field data-invalid={fieldState.invalid}>
                <FieldLabel htmlFor={`${tier}-base-url`}>Base URL</FieldLabel>
                <Input
                  id={`${tier}-base-url`}
                  placeholder={
                    tier === "balanced"
                      ? "https://api.example.com/v1"
                      : "留空表示不启用这一档"
                  }
                  aria-invalid={fieldState.invalid}
                  {...field}
                />
                <FieldError errors={[fieldState.error]} />
              </Field>
            )}
          />
          <Controller
            control={form.control}
            name={`${prefix}.model`}
            render={({ field, fieldState }) => (
              <Field data-invalid={fieldState.invalid}>
                <FieldLabel htmlFor={`${tier}-model`}>模型名</FieldLabel>
                <Input
                  id={`${tier}-model`}
                  aria-invalid={fieldState.invalid}
                  {...field}
                />
                <FieldError errors={[fieldState.error]} />
              </Field>
            )}
          />
          <Controller
            control={form.control}
            name={`${prefix}.input_cost_per_million_usd`}
            render={({ field, fieldState }) => (
              <Field data-invalid={fieldState.invalid}>
                <FieldLabel htmlFor={`${tier}-input-cost`}>
                  输入价格 / 百万 tokens（USD）
                </FieldLabel>
                <Input
                  id={`${tier}-input-cost`}
                  type="number"
                  min={0}
                  step="any"
                  aria-invalid={fieldState.invalid}
                  {...field}
                />
                <FieldError errors={[fieldState.error]} />
              </Field>
            )}
          />
          <Controller
            control={form.control}
            name={`${prefix}.output_cost_per_million_usd`}
            render={({ field, fieldState }) => (
              <Field data-invalid={fieldState.invalid}>
                <FieldLabel htmlFor={`${tier}-output-cost`}>
                  输出价格 / 百万 tokens（USD）
                </FieldLabel>
                <Input
                  id={`${tier}-output-cost`}
                  type="number"
                  min={0}
                  step="any"
                  aria-invalid={fieldState.invalid}
                  {...field}
                />
                <FieldError errors={[fieldState.error]} />
              </Field>
            )}
          />
        </div>
        <Controller
          control={form.control}
          name={`${prefix}.api_key`}
          render={({ field, fieldState }) => (
            <ApiKeyField
              idPrefix={tier}
              label="API Key"
              configured={value.api_key_configured}
              source={value.api_key_source}
              secretValue={field.value}
              clearChecked={clearApiKey}
              onSecretChange={field.onChange}
              onClearChange={(checked) =>
                form.setValue(`${prefix}.clear_api_key`, checked)
              }
              secretError={fieldState.error}
            />
          )}
        />
        <Controller
          control={form.control}
          name={`${prefix}.clear_api_key`}
          render={({ fieldState }) => <FieldError errors={[fieldState.error]} />}
        />
      </FieldGroup>
    </div>
  )
}

interface ApiKeyFieldProps {
  idPrefix: string
  label: string
  configured: boolean
  source: ConfigValueSource
  secretValue: string
  clearChecked: boolean
  onSecretChange: (value: string) => void
  onClearChange: (checked: boolean) => void
  secretError?: { message?: string }
  clearError?: { message?: string }
}

/**
 * API Key 输入区：状态徽章、来源徽章、密码框与清空勾选。
 */
function ApiKeyField({
  idPrefix,
  label,
  configured,
  source,
  secretValue,
  clearChecked,
  onSecretChange,
  onClearChange,
  secretError,
  clearError,
}: ApiKeyFieldProps) {
  const secretDisabled = clearChecked
  const clearDisabled = secretValue.trim().length > 0

  return (
    <Field data-invalid={!!secretError || !!clearError}>
      <div className="flex flex-wrap items-center gap-2">
        <FieldLabel htmlFor={`${idPrefix}-api-key`}>{label}</FieldLabel>
        <Badge variant={configured ? "default" : "secondary"}>
          {configured ? "已配置" : "未配置"}
        </Badge>
        <Badge variant="outline">{sourceLabel(source)}</Badge>
      </div>
      <Input
        id={`${idPrefix}-api-key`}
        type="password"
        autoComplete="new-password"
        placeholder="留空保留当前值"
        value={secretValue}
        disabled={secretDisabled}
        aria-invalid={!!secretError}
        onChange={(event) => onSecretChange(event.target.value)}
      />
      <FieldDescription>
        密钥不会从服务端回显。留空会保留当前值；填写新值会替换；勾选“清空密钥”会明确覆盖环境变量回退。
      </FieldDescription>
      <Field orientation="horizontal">
        <Checkbox
          id={`${idPrefix}-clear-api-key`}
          checked={clearChecked}
          disabled={clearDisabled}
          onCheckedChange={(checked) => onClearChange(checked === true)}
        />
        <FieldLabel htmlFor={`${idPrefix}-clear-api-key`}>清空密钥</FieldLabel>
      </Field>
      <FieldError errors={[secretError, clearError]} />
    </Field>
  )
}

/**
 * Chat 与 Embedding 模型配置表单卡片。
 *
 * Args:
 *   value: 当前 LLM 配置快照（不含密钥明文）
 *   onSaved: 保存成功后的回调，携带最新完整配置
 */
export function LlmConfigCard({ value, onSaved }: LlmConfigCardProps) {
  const form = useForm<LlmConfigFormValues>({
    resolver: zodResolver(llmConfigFormSchema),
    defaultValues: toFormValues(value),
  })

  useEffect(() => {
    form.reset(toFormValues(value))
  }, [value, form])

  const clearEmbeddingApiKey = useWatch({
    control: form.control,
    name: "clear_embedding_api_key",
  })

  const handleSubmit = async (data: LlmConfigFormValues) => {
    try {
      const next = await updateLlmSystemConfig(buildLlmUpdatePayload(data))
      toast.success("模型配置已保存")
      for (const tier of CHAT_TIERS) {
        form.setValue(`chat_${tier}.api_key`, "")
        form.setValue(`chat_${tier}.clear_api_key`, false)
      }
      form.setValue("embedding_api_key", "")
      form.setValue("clear_embedding_api_key", false)
      onSaved(next)
    } catch (error) {
      toast.error(readErrorMessage(error, "保存模型配置失败"))
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>模型配置</CardTitle>
        <CardDescription>管理 Chat 与 Embedding 服务端点及计费参数</CardDescription>
      </CardHeader>
      <form onSubmit={form.handleSubmit(handleSubmit)}>
        <CardContent>
          <div className="flex flex-col gap-5">
            {CHAT_TIERS.map((tier) => (
              <ChatTierFields
                key={tier}
                form={form}
                tier={tier}
                value={value[`chat_${tier}`]}
              />
            ))}

            <div className="rounded-xl border bg-muted/20 p-4 transition-colors">
              <div className="mb-4 flex flex-col gap-1 border-b border-border/60 pb-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-base font-semibold text-foreground">
                      Embedding 向量模型
                    </span>
                    <Badge variant="secondary" className="text-xs font-normal">
                      知识库召回
                    </Badge>
                  </div>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    用于知识库文档向量化与语义检索召回
                  </p>
                </div>
              </div>
              <FieldGroup className="gap-4">
                <div className="grid gap-4 sm:grid-cols-2">
                  <Controller
                    control={form.control}
                    name="embedding_base_url"
                    render={({ field, fieldState }) => (
                      <Field data-invalid={fieldState.invalid}>
                        <FieldLabel htmlFor="embedding-base-url">
                          Base URL
                        </FieldLabel>
                        <Input
                          id="embedding-base-url"
                          placeholder="https://api.example.com/v1"
                          aria-invalid={fieldState.invalid}
                          {...field}
                        />
                        <FieldError errors={[fieldState.error]} />
                      </Field>
                    )}
                  />
                  <Controller
                    control={form.control}
                    name="embedding_model"
                    render={({ field, fieldState }) => (
                      <Field data-invalid={fieldState.invalid}>
                        <FieldLabel htmlFor="embedding-model">模型名</FieldLabel>
                        <Input
                          id="embedding-model"
                          aria-invalid={fieldState.invalid}
                          {...field}
                        />
                        <FieldError errors={[fieldState.error]} />
                      </Field>
                    )}
                  />
                </div>
                <Controller
                  control={form.control}
                  name="embedding_api_key"
                  render={({ field, fieldState }) => (
                    <ApiKeyField
                      idPrefix="embedding"
                      label="API Key"
                      configured={value.embedding_api_key_configured}
                      source={value.embedding_api_key_source}
                      secretValue={field.value}
                      clearChecked={clearEmbeddingApiKey}
                      onSecretChange={field.onChange}
                      onClearChange={(checked) =>
                        form.setValue("clear_embedding_api_key", checked)
                      }
                      secretError={fieldState.error}
                    />
                  )}
                />
                <Controller
                  control={form.control}
                  name="clear_embedding_api_key"
                  render={({ fieldState }) => (
                    <FieldError errors={[fieldState.error]} />
                  )}
                />
              </FieldGroup>
            </div>
          </div>
        </CardContent>
        <CardFooter className="justify-end border-t pt-4">
          <Button type="submit" disabled={form.formState.isSubmitting}>
            {form.formState.isSubmitting && <Spinner data-icon="inline-start" />}
            保存模型配置
          </Button>
        </CardFooter>
      </form>
    </Card>
  )
}
