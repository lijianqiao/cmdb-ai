/** 系统配置表单规则与载荷构造单测 */

import { describe, expect, it } from "vitest"

import {
  buildLlmUpdatePayload,
  llmConfigFormSchema,
  operationsConfigFormSchema,
  type LlmConfigFormValues,
} from "./systemConfigFormSchemas"

function validLlmForm(
  overrides: Partial<LlmConfigFormValues> = {},
): LlmConfigFormValues {
  return {
    chat_base_url: "https://chat.example/v1",
    chat_api_key: "",
    clear_chat_api_key: false,
    chat_model: "chat",
    chat_input_cost_per_million_usd: 0,
    chat_output_cost_per_million_usd: 0,
    embedding_base_url: "https://embedding.example/v1",
    embedding_api_key: "",
    clear_embedding_api_key: false,
    embedding_model: "embedding",
    ...overrides,
  }
}

const validOperationsForm = {
  hitl_notify_auto_approve: false,
  monitor_probe_timeout_seconds: 3,
  monitor_sweep_interval_seconds: 30,
  cmdb_diff_interval_seconds: 3600,
}

describe("系统配置表单规则", () => {
  it("拒绝非 HTTP URL 和负数费用", () => {
    const result = llmConfigFormSchema.safeParse({
      chat_base_url: "ftp://llm.example/v1",
      chat_api_key: "",
      clear_chat_api_key: false,
      chat_model: "chat",
      chat_input_cost_per_million_usd: -1,
      chat_output_cost_per_million_usd: 0,
      embedding_base_url: "https://embedding.example/v1",
      embedding_api_key: "",
      clear_embedding_api_key: false,
      embedding_model: "embedding",
    })
    expect(result.success).toBe(false)
  })

  it("密钥输入留空时不发送该字段", () => {
    const payload = buildLlmUpdatePayload(
      validLlmForm({
        chat_api_key: "",
        clear_chat_api_key: false,
      }),
    )
    expect(payload).not.toHaveProperty("chat_api_key")
  })

  it("明确清空时只发送 clear 标记", () => {
    const payload = buildLlmUpdatePayload(
      validLlmForm({
        chat_api_key: "",
        clear_chat_api_key: true,
      }),
    )
    expect(payload.clear_chat_api_key).toBe(true)
    expect(payload).not.toHaveProperty("chat_api_key")
  })

  it("非空密钥且未清空时发送替换值", () => {
    const payload = buildLlmUpdatePayload(
      validLlmForm({
        chat_api_key: "  sk-test  ",
        clear_chat_api_key: false,
      }),
    )
    expect(payload.chat_api_key).toBe("sk-test")
  })

  it("拒绝同时提交新密钥与清空标记", () => {
    const result = llmConfigFormSchema.safeParse(
      validLlmForm({
        chat_api_key: "sk-test",
        clear_chat_api_key: true,
      }),
    )
    expect(result.success).toBe(false)
  })

  it("拒绝含凭据的 Base URL", () => {
    const result = llmConfigFormSchema.safeParse(
      validLlmForm({
        chat_base_url: "https://user:password@host/v1",
      }),
    )
    expect(result.success).toBe(false)
  })

  it.each([
    Number.NaN,
    Number.POSITIVE_INFINITY,
    -0.01,
  ])("拒绝无效费用 %s", (value) => {
    const result = llmConfigFormSchema.safeParse(
      validLlmForm({
        chat_input_cost_per_million_usd: value,
      }),
    )
    expect(result.success).toBe(false)
  })

  it.each([0, 30])("接受探测超时边界值 %s", (value) => {
    const result = operationsConfigFormSchema.safeParse({
      ...validOperationsForm,
      monitor_probe_timeout_seconds: value,
    })
    expect(result.success).toBe(value === 0 ? false : true)
  })

  it.each([0, 31])("拒绝越界探测超时 %s", (value) => {
    const result = operationsConfigFormSchema.safeParse({
      ...validOperationsForm,
      monitor_probe_timeout_seconds: value,
    })
    expect(result.success).toBe(false)
  })

  it.each([5, 3600])("接受巡检间隔边界值 %s", (value) => {
    const result = operationsConfigFormSchema.safeParse({
      ...validOperationsForm,
      monitor_sweep_interval_seconds: value,
    })
    expect(result.success).toBe(true)
  })

  it.each([4, 3601])("拒绝越界巡检间隔 %s", (value) => {
    const result = operationsConfigFormSchema.safeParse({
      ...validOperationsForm,
      monitor_sweep_interval_seconds: value,
    })
    expect(result.success).toBe(false)
  })

  it.each([60, 86400])("接受 CMDB 差异巡检边界值 %s", (value) => {
    const result = operationsConfigFormSchema.safeParse({
      ...validOperationsForm,
      cmdb_diff_interval_seconds: value,
    })
    expect(result.success).toBe(true)
  })

  it.each([59, 86401])("拒绝越界 CMDB 差异巡检 %s", (value) => {
    const result = operationsConfigFormSchema.safeParse({
      ...validOperationsForm,
      cmdb_diff_interval_seconds: value,
    })
    expect(result.success).toBe(false)
  })
})
