/** 系统配置表单 Zod 校验与 LLM 更新载荷构造 */

import { z } from "zod"

import {
  CHAT_TIERS,
  type ChatTierUpdate,
  type LlmSystemConfigUpdate,
} from "@/types/system-config"

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

/** 便宜档 / 强档可以整档留空表示"不配置"，所以 base_url 允许空串 */
const optionalHttpBaseUrlSchema = z
  .string()
  .max(2048)
  .transform((value, ctx) => {
    const trimmed = value.trim()
    if (!trimmed) return ""
    try {
      return normalizeBaseUrl(trimmed)
    } catch (error) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: error instanceof Error ? error.message : "Base URL 无效",
      })
      return z.NEVER
    }
  })

const optionalChatTierSchema = z.object({
  base_url: optionalHttpBaseUrlSchema,
  api_key: z.string().max(4096).default(""),
  clear_api_key: z.boolean(),
  model: z.string().max(200),
  input_cost_per_million_usd: costSchema,
  output_cost_per_million_usd: costSchema,
})

/** 平衡档是其它两档的回退目标，不允许留空 */
const requiredChatTierSchema = optionalChatTierSchema.extend({
  base_url: httpBaseUrlSchema,
  model: z.string().min(1, "请输入 Chat 模型名").max(200),
})

const llmConfigFormObjectSchema = z.object({
  chat_fast: optionalChatTierSchema,
  chat_balanced: requiredChatTierSchema,
  chat_strong: optionalChatTierSchema,
  embedding_base_url: httpBaseUrlSchema,
  embedding_api_key: z.string().max(4096).default(""),
  clear_embedding_api_key: z.boolean(),
  embedding_model: z.string().min(1, "请输入 Embedding 模型名").max(200),
})

/** LLM 配置表单校验 schema */
export const llmConfigFormSchema = llmConfigFormObjectSchema.superRefine(
  (data, ctx) => {
    for (const tier of CHAT_TIERS) {
      const value = data[`chat_${tier}`]
      if (value.clear_api_key && value.api_key.trim()) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: [`chat_${tier}`, "api_key"],
          message: "不能同时提交新的 api_key 与 clear_api_key=true",
        })
      }
      // base_url 的 transform 失败时这里拿到的不是字符串，跳过即可——
      // 那条错误已经报出来了，再叠一条"要么都填要么都留空"只会让人更懵
      if (typeof value.base_url !== "string" || typeof value.model !== "string") {
        continue
      }
      // 半份配置发不出请求：有地址没模型名（或反过来）比整档留空更难排查
      const hasBaseUrl = Boolean(value.base_url.trim())
      const hasModel = Boolean(value.model.trim())
      if (hasBaseUrl !== hasModel) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: [`chat_${tier}`, hasBaseUrl ? "model" : "base_url"],
          message: "Base URL 与模型名要么都填，要么都留空（留空即不启用这一档）",
        })
      }
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
  monitor_event_retention_days: z.coerce
    .number()
    .int()
    .min(1, "监控日志保留天数不能小于 1 天")
    .max(90, "监控日志保留天数不能超过 90 天"),
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
function buildTierPayload(tier: LlmConfigFormValues["chat_balanced"]): ChatTierUpdate {
  const payload: ChatTierUpdate = {
    base_url: tier.base_url,
    model: tier.model,
    input_cost_per_million_usd: tier.input_cost_per_million_usd,
    output_cost_per_million_usd: tier.output_cost_per_million_usd,
    clear_api_key: tier.clear_api_key,
  }
  const key = tier.api_key.trim()
  if (key && !tier.clear_api_key) payload.api_key = key
  return payload
}

export function buildLlmUpdatePayload(
  form: LlmConfigFormValues,
): LlmSystemConfigUpdate {
  const payload: LlmSystemConfigUpdate = {
    chat_fast: buildTierPayload(form.chat_fast),
    chat_balanced: buildTierPayload(form.chat_balanced),
    chat_strong: buildTierPayload(form.chat_strong),
    embedding_base_url: form.embedding_base_url,
    embedding_model: form.embedding_model,
    clear_embedding_api_key: form.clear_embedding_api_key,
  }
  const embeddingKey = form.embedding_api_key.trim()
  if (embeddingKey && !form.clear_embedding_api_key) {
    payload.embedding_api_key = embeddingKey
  }
  return payload
}
