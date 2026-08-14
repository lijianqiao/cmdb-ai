/** LLM 与 Embedding 模型配置卡片 */

import { useEffect } from "react"
import { Controller, useForm, useWatch } from "react-hook-form"
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
  FieldLegend,
  FieldSeparator,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { updateLlmSystemConfig } from "@/lib/system-config-api"
import type {
  ConfigValueSource,
  LlmSystemConfig,
  SystemConfigData,
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

function toFormValues(value: LlmSystemConfig): LlmConfigFormValues {
  return {
    chat_base_url: value.chat_base_url,
    chat_api_key: "",
    clear_chat_api_key: false,
    chat_model: value.chat_model,
    chat_input_cost_per_million_usd: value.chat_input_cost_per_million_usd,
    chat_output_cost_per_million_usd: value.chat_output_cost_per_million_usd,
    embedding_base_url: value.embedding_base_url,
    embedding_api_key: "",
    clear_embedding_api_key: false,
    embedding_model: value.embedding_model,
  }
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

  const clearChatApiKey = useWatch({
    control: form.control,
    name: "clear_chat_api_key",
  })
  const clearEmbeddingApiKey = useWatch({
    control: form.control,
    name: "clear_embedding_api_key",
  })

  const handleSubmit = async (data: LlmConfigFormValues) => {
    try {
      const next = await updateLlmSystemConfig(buildLlmUpdatePayload(data))
      toast.success("模型配置已保存")
      form.setValue("chat_api_key", "")
      form.setValue("embedding_api_key", "")
      form.setValue("clear_chat_api_key", false)
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
          <FieldGroup>
            <FieldSet>
              <FieldLegend>Chat</FieldLegend>
              <FieldGroup className="gap-4">
                <div className="grid gap-4 sm:grid-cols-2">
                  <Controller
                    control={form.control}
                    name="chat_base_url"
                    render={({ field, fieldState }) => (
                      <Field data-invalid={fieldState.invalid}>
                        <FieldLabel htmlFor="chat-base-url">Base URL</FieldLabel>
                        <Input
                          id="chat-base-url"
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
                    name="chat_model"
                    render={({ field, fieldState }) => (
                      <Field data-invalid={fieldState.invalid}>
                        <FieldLabel htmlFor="chat-model">模型名</FieldLabel>
                        <Input
                          id="chat-model"
                          aria-invalid={fieldState.invalid}
                          {...field}
                        />
                        <FieldError errors={[fieldState.error]} />
                      </Field>
                    )}
                  />
                  <Controller
                    control={form.control}
                    name="chat_input_cost_per_million_usd"
                    render={({ field, fieldState }) => (
                      <Field data-invalid={fieldState.invalid}>
                        <FieldLabel htmlFor="chat-input-cost">
                          输入价格 / 百万 tokens（USD）
                        </FieldLabel>
                        <Input
                          id="chat-input-cost"
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
                    name="chat_output_cost_per_million_usd"
                    render={({ field, fieldState }) => (
                      <Field data-invalid={fieldState.invalid}>
                        <FieldLabel htmlFor="chat-output-cost">
                          输出价格 / 百万 tokens（USD）
                        </FieldLabel>
                        <Input
                          id="chat-output-cost"
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
                  name="chat_api_key"
                  render={({ field, fieldState }) => (
                    <ApiKeyField
                      idPrefix="chat"
                      label="API Key"
                      configured={value.chat_api_key_configured}
                      source={value.chat_api_key_source}
                      secretValue={field.value}
                      clearChecked={clearChatApiKey}
                      onSecretChange={field.onChange}
                      onClearChange={(checked) =>
                        form.setValue("clear_chat_api_key", checked)
                      }
                      secretError={fieldState.error}
                    />
                  )}
                />
                <Controller
                  control={form.control}
                  name="clear_chat_api_key"
                  render={({ fieldState }) => (
                    <FieldError errors={[fieldState.error]} />
                  )}
                />
              </FieldGroup>
            </FieldSet>

            <FieldSeparator />

            <FieldSet>
              <FieldLegend>Embedding</FieldLegend>
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
            </FieldSet>
          </FieldGroup>
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
