/** 系统配置表单规则与载荷构造单测 */

import { describe, expect, it } from "vitest"

import {
  buildLlmUpdatePayload,
  llmConfigFormSchema,
  operationsConfigFormSchema,
  type LlmConfigFormValues,
} from "./systemConfigFormSchemas"

type TierForm = LlmConfigFormValues["chat_balanced"]

function tierForm(overrides: Partial<TierForm> = {}): TierForm {
  return {
    base_url: "https://chat.example/v1",
    api_key: "",
    clear_api_key: false,
    model: "chat",
    input_cost_per_million_usd: 0,
    output_cost_per_million_usd: 0,
    ...overrides,
  }
}

/** 未配置的档：整档留空 */
function emptyTierForm(): TierForm {
  return tierForm({ base_url: "", model: "" })
}

function validLlmForm(
  balancedOverrides: Partial<TierForm> = {},
  overrides: Partial<LlmConfigFormValues> = {},
): LlmConfigFormValues {
  return {
    chat_fast: emptyTierForm(),
    chat_balanced: tierForm(balancedOverrides),
    chat_strong: emptyTierForm(),
    embedding_base_url: "https://embedding.example/v1",
    embedding_api_key: "",
    clear_embedding_api_key: false,
    embedding_model: "embedding",
    ...overrides,
  }
}

const validOperationsForm = {
  monitor_probe_timeout_seconds: 3,
  monitor_sweep_interval_seconds: 30,
  cmdb_diff_interval_seconds: 3600,
  monitor_event_retention_days: 7,
}

describe("系统配置表单规则", () => {
  it("拒绝非 HTTP URL 和负数费用", () => {
    const result = llmConfigFormSchema.safeParse(
      validLlmForm({
        base_url: "ftp://llm.example/v1",
        input_cost_per_million_usd: -1,
      }),
    )
    expect(result.success).toBe(false)
  })

  it("密钥输入留空时不发送该字段", () => {
    const payload = buildLlmUpdatePayload(validLlmForm())
    expect(payload.chat_balanced).not.toHaveProperty("api_key")
  })

  it("明确清空时只发送 clear 标记", () => {
    const payload = buildLlmUpdatePayload(
      validLlmForm({ api_key: "", clear_api_key: true }),
    )
    expect(payload.chat_balanced.clear_api_key).toBe(true)
    expect(payload.chat_balanced).not.toHaveProperty("api_key")
  })

  it("非空密钥且未清空时发送替换值", () => {
    const payload = buildLlmUpdatePayload(
      validLlmForm({ api_key: "  sk-test  " }),
    )
    expect(payload.chat_balanced.api_key).toBe("sk-test")
  })

  it("各档密钥互不干扰", () => {
    const payload = buildLlmUpdatePayload(
      validLlmForm({ api_key: "sk-balanced" }, {
        chat_strong: tierForm({
          base_url: "https://strong.example/v1",
          model: "strong",
          api_key: "sk-strong",
        }),
      }),
    )
    expect(payload.chat_balanced.api_key).toBe("sk-balanced")
    expect(payload.chat_strong.api_key).toBe("sk-strong")
    expect(payload.chat_fast).not.toHaveProperty("api_key")
  })

  it("拒绝同时提交新密钥与清空标记", () => {
    const result = llmConfigFormSchema.safeParse(
      validLlmForm({ api_key: "sk-test", clear_api_key: true }),
    )
    expect(result.success).toBe(false)
  })

  it("拒绝含凭据的 Base URL", () => {
    const result = llmConfigFormSchema.safeParse(
      validLlmForm({ base_url: "https://user:password@host/v1" }),
    )
    expect(result.success).toBe(false)
  })

  it("允许便宜档与强档整档留空（表示不启用）", () => {
    const result = llmConfigFormSchema.safeParse(validLlmForm())
    expect(result.success).toBe(true)
  })

  it("拒绝半份配置：有 Base URL 没模型名", () => {
    // 这种状态发不出请求，比整档留空更难排查
    const result = llmConfigFormSchema.safeParse(
      validLlmForm({}, {
        chat_fast: tierForm({ base_url: "https://fast.example/v1", model: "" }),
      }),
    )
    expect(result.success).toBe(false)
  })

  it("平衡档不允许留空——它是其它两档的回退目标", () => {
    const result = llmConfigFormSchema.safeParse(
      validLlmForm({ base_url: "", model: "" }),
    )
    expect(result.success).toBe(false)
  })

  it.each([
    Number.NaN,
    Number.POSITIVE_INFINITY,
    -0.01,
  ])("拒绝无效费用 %s", (value) => {
    const result = llmConfigFormSchema.safeParse(
      validLlmForm({ input_cost_per_million_usd: value }),
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

  it.each([1, 90])("接受监控日志保留天数边界值 %s", (value) => {
    const result = operationsConfigFormSchema.safeParse({
      ...validOperationsForm,
      monitor_event_retention_days: value,
    })
    expect(result.success).toBe(true)
  })

  it.each([0, 91])("拒绝越界监控日志保留天数 %s", (value) => {
    const result = operationsConfigFormSchema.safeParse({
      ...validOperationsForm,
      monitor_event_retention_days: value,
    })
    expect(result.success).toBe(false)
  })
})
