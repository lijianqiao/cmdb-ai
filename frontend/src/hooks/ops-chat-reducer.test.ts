/** ops chat reducer / 门控纯函数单测：历史映射、WS 合并、竞态与去重 */

import { describe, expect, it } from "vitest"

import {
  mapHistoryToItems,
  reduceOpsChat,
  shouldEnableAgentWs,
  shouldSynthesizeSendError,
  type OpsChatState,
} from "./use-ops-chat"
import type {
  AgentMessage,
  AgentSessionSnapshot,
  HitlProposalSafeSummary,
} from "@/types/agent"

function buildSnapshot(
  overrides: Partial<AgentSessionSnapshot> = {},
): AgentSessionSnapshot {
  return Object.assign(
    {
      messages: [],
      proposals: [],
      children: [],
      has_more_messages: false,
      next_before_message_id: null,
    },
    overrides,
  )
}

function message(
  overrides: Partial<AgentMessage> &
    Pick<AgentMessage, "id" | "role" | "content">,
): AgentMessage {
  return Object.assign(
    {
      session_id: 1,
      tool_call_id: null,
      tool_calls: null,
      created_at: "2026-08-14T00:00:00Z",
    },
    overrides,
  )
}

function proposal(
  overrides: Partial<HitlProposalSafeSummary> &
    Pick<HitlProposalSafeSummary, "proposal_id" | "status">,
): HitlProposalSafeSummary {
  return Object.assign(
    {
      action_type: "notify",
      status_reason: null,
      reason: "test",
      asset_id: null,
      created_at: "2026-08-14T00:00:00Z",
      execution_started_at: null,
      resolved_at: null,
    },
    overrides,
  )
}

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

  it("hitl_resolved 携带 result_excerpt 时写入时间线", () => {
    const state = reduceOpsChat(empty, {
      type: "ws",
      message: {
        type: "hitl_resolved",
        payload: {
          proposal_id: 9,
          status: "executed",
          result_excerpt: "Cisco IOS XE Software, Version 17.9.4a",
        },
      },
    })
    expect(state.items).toHaveLength(1)
    expect(state.items[0]).toMatchObject({
      kind: "hitl",
      proposalId: 9,
      status: "executed",
      resultExcerpt: "Cisco IOS XE Software, Version 17.9.4a",
    })
  })

  it("updates one child card by child_id", () => {
    let state = reduceOpsChat({ items: [] }, {
      type: "ws",
      message: {
        type: "child_status",
        payload: { child_id: "c1", role: "ops_explorer", status: "RUNNING" },
      },
    })
    state = reduceOpsChat(state, {
      type: "ws",
      message: {
        type: "child_status",
        payload: { child_id: "c1", role: "ops_explorer", status: "COMPLETED" },
      },
    })
    expect(state.items.filter((item) => item.kind === "child")).toHaveLength(1)
    expect(state.items[0]).toMatchObject({ status: "COMPLETED" })
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

describe("reduceOpsChat snapshot_loaded", () => {
  it("hydrates messages and pending proposals with stable ids", () => {
    const state = reduceOpsChat(
      { items: [] },
      {
        type: "snapshot_loaded",
        replace: true,
        snapshot: buildSnapshot({
          messages: [message({ id: 10, role: "assistant", content: "done" })],
          proposals: [proposal({ proposal_id: 7, status: "PENDING" })],
        }),
      },
    )
    expect(state.items.map((item) => item.id)).toEqual(["message:10", "hitl:7"])
  })

  it("同一 snapshot 重放不重复条目", () => {
    const action = {
      type: "snapshot_loaded" as const,
      replace: true,
      snapshot: buildSnapshot({
        messages: [message({ id: 10, role: "assistant", content: "done" })],
        proposals: [proposal({ proposal_id: 7, status: "PENDING" })],
      }),
    }
    let state = reduceOpsChat({ items: [] }, action)
    state = reduceOpsChat(state, action)
    expect(state.items).toHaveLength(2)
    expect(state.items.map((item) => item.id)).toEqual(["message:10", "hitl:7"])
  })

  it("older page 前插更早消息", () => {
    const base = reduceOpsChat(
      { items: [] },
      {
        type: "snapshot_loaded",
        replace: true,
        snapshot: buildSnapshot({
          messages: [message({ id: 20, role: "user", content: "newer" })],
        }),
      },
    )
    const state = reduceOpsChat(base, {
      type: "snapshot_loaded",
      replace: false,
      snapshot: buildSnapshot({
        messages: [message({ id: 10, role: "user", content: "older" })],
      }),
    })
    expect(state.items.map((item) => item.id)).toEqual([
      "message:10",
      "message:20",
    ])
  })

  it("replace 保留 pending optimistic user", () => {
    const withOptimistic = reduceOpsChat(empty, {
      type: "user_sent",
      clientId: "local-user-1",
      content: "发送中",
    })
    const state = reduceOpsChat(withOptimistic, {
      type: "snapshot_loaded",
      replace: true,
      snapshot: buildSnapshot({
        messages: [message({ id: 5, role: "assistant", content: "reply" })],
      }),
    })
    expect(state.items.map((item) => item.id)).toEqual([
      "message:5",
      "local-user-1",
    ])
    expect(state.items[1]).toMatchObject({
      kind: "user",
      content: "发送中",
    })
  })

  it("服务端最终 assistant 替换 streaming 临时项", () => {
    let state = reduceOpsChat(empty, {
      type: "ws",
      message: {
        type: "assistant_delta",
        payload: { text: "半截", done: false },
      },
    })
    expect(state.items[0]).toMatchObject({
      kind: "assistant",
      streaming: true,
      content: "半截",
    })
    state = reduceOpsChat(state, {
      type: "snapshot_loaded",
      replace: true,
      snapshot: buildSnapshot({
        messages: [
          message({ id: 99, role: "assistant", content: "完整回答" }),
        ],
      }),
    })
    expect(state.items).toHaveLength(1)
    expect(state.items[0]).toMatchObject({
      kind: "assistant",
      id: "message:99",
      content: "完整回答",
      streaming: false,
    })
  })
})
