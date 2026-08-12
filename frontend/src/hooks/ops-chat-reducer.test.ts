/** ops chat reducer / 门控纯函数单测：历史映射、WS 合并、竞态与去重 */

import { describe, expect, it } from "vitest"

import {
  mapHistoryToItems,
  reduceOpsChat,
  shouldEnableAgentWs,
  shouldSynthesizeSendError,
  type OpsChatState,
} from "./use-ops-chat"
import type { AgentMessage } from "@/types/agent"

const empty: OpsChatState = { items: [] }

describe("mapHistoryToItems", () => {
  it("把 user/assistant/tool_calls 映射为时间线，跳过 tool 结果", () => {
    const rows: AgentMessage[] = [
      {
        id: 1,
        session_id: 9,
        role: "user",
        content: "你好",
        tool_call_id: null,
        tool_calls: null,
        created_at: "2026-01-01T00:00:00Z",
      },
      {
        id: 2,
        session_id: 9,
        role: "assistant",
        content: "",
        tool_call_id: null,
        tool_calls: [{ id: "tc1", name: "search_kb", arguments: {} }],
        created_at: "2026-01-01T00:00:01Z",
      },
      {
        id: 3,
        session_id: 9,
        role: "tool",
        content: "命中 1 条",
        tool_call_id: "tc1",
        tool_calls: null,
        created_at: "2026-01-01T00:00:02Z",
      },
      {
        id: 4,
        session_id: 9,
        role: "assistant",
        content: "根据文档…",
        tool_call_id: null,
        tool_calls: null,
        created_at: "2026-01-01T00:00:03Z",
      },
    ]
    const items = mapHistoryToItems(rows)
    expect(items.map((i) => i.kind)).toEqual([
      "user",
      "tool_call",
      "assistant",
    ])
    expect(items[1]).toMatchObject({ kind: "tool_call", name: "search_kb" })
  })
})

describe("reduceOpsChat", () => {
  it("拼接 assistant_delta 并在 done 后结束 streaming", () => {
    let state = reduceOpsChat(empty, {
      type: "ws",
      message: {
        type: "assistant_delta",
        payload: { text: "你好", done: false },
      },
    })
    state = reduceOpsChat(state, {
      type: "ws",
      message: {
        type: "assistant_delta",
        payload: { text: "世界", done: true },
      },
    })
    expect(state.items).toHaveLength(1)
    expect(state.items[0]).toMatchObject({
      kind: "assistant",
      content: "你好世界",
      streaming: false,
    })
  })

  it("hitl_pending 后 hitl_resolved 更新同一提案", () => {
    let state = reduceOpsChat(empty, {
      type: "ws",
      message: {
        type: "hitl_pending",
        payload: {
          proposal_id: 7,
          action_type: "notify",
          status: "pending",
          reason: "需审批",
        },
      },
    })
    state = reduceOpsChat(state, {
      type: "ws",
      message: {
        type: "hitl_resolved",
        payload: { proposal_id: 7, status: "approved" },
      },
    })
    expect(state.items).toHaveLength(1)
    expect(state.items[0]).toMatchObject({
      kind: "hitl",
      proposalId: 7,
      status: "approved",
    })
  })

  it("turn_done 只结束 streaming，不改其它条目", () => {
    let state = reduceOpsChat(empty, {
      type: "ws",
      message: {
        type: "assistant_delta",
        payload: { text: "半截", done: false },
      },
    })
    state = reduceOpsChat(state, {
      type: "ws",
      message: { type: "turn_done", payload: { reason: "final" } },
    })
    expect(state.items).toHaveLength(1)
    expect(state.items[0]).toMatchObject({
      kind: "assistant",
      content: "半截",
      streaming: false,
    })
  })
})

describe("shouldEnableAgentWs", () => {
  it("仅当 historyReadySessionId 等于当前 session 时放行", () => {
    expect(shouldEnableAgentWs(3, null)).toBe(false)
    expect(shouldEnableAgentWs(3, 2)).toBe(false)
    expect(shouldEnableAgentWs(null, 3)).toBe(false)
    expect(shouldEnableAgentWs(3, 3)).toBe(true)
  })
})

describe("shouldSynthesizeSendError", () => {
  it("WS 已推 error 时不再合成 HTTP catch 错误行", () => {
    expect(shouldSynthesizeSendError(false)).toBe(true)
    expect(shouldSynthesizeSendError(true)).toBe(false)
  })
})
