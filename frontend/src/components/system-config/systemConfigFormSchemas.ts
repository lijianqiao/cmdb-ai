/** 系统配置表单 Zod 校验与 LLM 更新载荷构造 */

import { z } from "zod"

import type { LlmSystemConfigUpdate } from "@/types/system-config"

const costSchema = z.coerce.number().finite().nonnegative()

/**
 * 规范化并校验 LLM Base URL，与后端 normalize_base_url 语义一致。
 *
 * Args:
 *   value: 原始 URL 字符串
 *
 * Returns:
 *   去除首尾空白与末尾斜杠后的合法 URL
 */
export function normalizeBaseUrl(value: string): string {
  let trimmed = value.trim()
  if (trimmed.endsWith("/")) {
    trimmed = trimmed.replace(/\/+$/, "")
  }

  let parsed: URL
  try {
    parsed = new URL(trimmed)
  } catch {
    throw new Error("Base URL 必须包含主机名")
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("Base URL 仅支持 http 或 https 协议")
  }
  if (!parsed.hostname) {
    throw new Error("Base URL 必须包含主机名")
  }
  if (parsed.username || parsed.password) {
    throw new Error("Base URL 不能包含用户名或密码")
  }
  if (parsed.search || parsed.hash) {
    throw new Error("Base URL 不能包含查询参数或片段")
  }

  return trimmed
}

const httpBaseUrlSchema = z
  .string()
  .min(1, "请输入 Base URL")
  .max(2048)
  .transform((value, ctx) => {
    try {
      return normalizeBaseUrl(value)
    } catch (error) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: error instanceof Error ? error.message : "Base URL 无效",
      })
      return z.NEVER
    }
  })

const llmConfigFormObjectSchema = z.object({
  chat_base_url: httpBaseUrlSchema,
  chat_api_key: z.string().max(4096).default(""),
  clear_chat_api_key: z.boolean(),
  chat_model: z.string().min(1, "请输入 Chat 模型名").max(200),
  chat_input_cost_per_million_usd: costSchema,
  chat_output_cost_per_million_usd: costSchema,
  embedding_base_url: httpBaseUrlSchema,
  embedding_api_key: z.string().max(4096).default(""),
  clear_embedding_api_key: z.boolean(),
  embedding_model: z.string().min(1, "请输入 Embedding 模型名").max(200),
})

/** LLM 配置表单校验 schema */
export const llmConfigFormSchema = llmConfigFormObjectSchema.superRefine(
  (data, ctx) => {
    if (data.clear_chat_api_key && data.chat_api_key.trim()) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["chat_api_key"],
        message: "不能同时提交新的 chat_api_key 与 clear_chat_api_key=true",
      })
    }
    if (data.clear_embedding_api_key && data.embedding_api_key.trim()) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["embedding_api_key"],
        message:
          "不能同时提交新的 embedding_api_key 与 clear_embedding_api_key=true",
      })
    }
  },
)

/** 运行参数表单校验 schema */
export const operationsConfigFormSchema = z.object({
  hitl_notify_auto_approve: z.boolean(),
  monitor_probe_timeout_seconds: z.coerce
    .number()
    .finite()
    .gt(0, "探测超时必须大于 0 秒")
    .max(30, "探测超时不能超过 30 秒"),
  monitor_sweep_interval_seconds: z.coerce
    .number()
    .finite()
    .min(5, "巡检间隔不能小于 5 秒")
    .max(3600, "巡检间隔不能超过 3600 秒"),
  cmdb_diff_interval_seconds: z.coerce
    .number()
    .finite()
    .min(60, "CMDB 差异巡检间隔不能小于 60 秒")
    .max(86400, "CMDB 差异巡检间隔不能超过 86400 秒"),
})

export type LlmConfigFormValues = z.infer<typeof llmConfigFormSchema>
export type OperationsConfigFormValues = z.infer<
  typeof operationsConfigFormSchema
>

/**
 * 将 LLM 表单值转换为 API 更新载荷。
 *
 * 密钥留空且未勾选清空时不发送对应字段（保留现有值）；
 * 非空密钥且未勾选清空时发送替换值；勾选清空时仅发送 clear 标记。
 *
 * Args:
 *   form: 已通过 llmConfigFormSchema 校验的表单值
 *
 * Returns:
 *   符合后端 LlmSystemConfigUpdate 契约的载荷
 */
export function buildLlmUpdatePayload(
  form: LlmConfigFormValues,
): LlmSystemConfigUpdate {
  const payload: LlmSystemConfigUpdate = {
    chat_base_url: form.chat_base_url,
    chat_model: form.chat_model,
    chat_input_cost_per_million_usd: form.chat_input_cost_per_million_usd,
    chat_output_cost_per_million_usd: form.chat_output_cost_per_million_usd,
    embedding_base_url: form.embedding_base_url,
    embedding_model: form.embedding_model,
    clear_chat_api_key: form.clear_chat_api_key,
    clear_embedding_api_key: form.clear_embedding_api_key,
  }
  const chatKey = form.chat_api_key.trim()
  const embeddingKey = form.embedding_api_key.trim()
  if (chatKey && !form.clear_chat_api_key) payload.chat_api_key = chatKey
  if (embeddingKey && !form.clear_embedding_api_key) {
    payload.embedding_api_key = embeddingKey
  }
  return payload
}
