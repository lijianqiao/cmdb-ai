/** agent-ws 纯函数单测：URL 构造、消息解析、重连退避 */

import { describe, expect, it } from "vitest"

import {
  buildAgentWsUrl,
  nextReconnectDelay,
  parseAgentWsMessage,
} from "./agent-ws"

describe("buildAgentWsUrl", () => {
  it("用当前 host 拼出带 access_token 的 ws URL", () => {
    const url = buildAgentWsUrl(42, "tok en+1", {
      protocol: "http:",
      host: "localhost:5173",
    })
    expect(url).toBe(
      "ws://localhost:5173/api/v1/ws/agent/42?access_token=tok%20en%2B1"
    )
  })

  it("https 页面使用 wss", () => {
    const url = buildAgentWsUrl(1, "abc", {
      protocol: "https:",
      host: "example.com",
    })
    expect(url.startsWith("wss://example.com/")).toBe(true)
  })
})

describe("parseAgentWsMessage", () => {
  it("解析合法判别式消息", () => {
    const msg = parseAgentWsMessage(
      JSON.stringify({ type: "assistant_delta", payload: { text: "hi", done: false } })
    )
    expect(msg).toEqual({
      type: "assistant_delta",
      payload: { text: "hi", done: false },
    })
  })

  it("缺 payload 时补空对象", () => {
    const msg = parseAgentWsMessage(JSON.stringify({ type: "turn_done" }))
    expect(msg).toEqual({ type: "turn_done", payload: {} })
  })

  it("非法 JSON / 未知 type / 非对象返回 null", () => {
    expect(parseAgentWsMessage("not-json")).toBeNull()
    expect(parseAgentWsMessage(JSON.stringify({ type: "unknown" }))).toBeNull()
    expect(parseAgentWsMessage(JSON.stringify([]))).toBeNull()
    expect(parseAgentWsMessage(JSON.stringify({ type: "error", payload: "x" }))).toBeNull()
  })
})

describe("nextReconnectDelay", () => {
  it("指数退避并从 1s 起算，封顶 30s", () => {
    expect(nextReconnectDelay(0)).toBe(1_000)
    expect(nextReconnectDelay(1)).toBe(2_000)
    expect(nextReconnectDelay(2)).toBe(4_000)
    expect(nextReconnectDelay(3)).toBe(8_000)
    expect(nextReconnectDelay(4)).toBe(16_000)
    expect(nextReconnectDelay(5)).toBe(30_000)
    expect(nextReconnectDelay(10)).toBe(30_000)
  })
})
