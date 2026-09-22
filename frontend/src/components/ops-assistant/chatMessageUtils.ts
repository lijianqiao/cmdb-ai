/** 运维助手消息时间线归组工具函数
 *
 * 将扁平的 OpsChatItem 列表按问答轮次（Turn）归组：
 * 每一轮对话中，仅将该轮的最后一个 assistant 消息作为最外层展示的「最终回答」，
 * 最终回答之前的所有中间过程（包括中间 assistant 文本、工具调用、子 Agent、HITL 审批）全部归入 processItems 中折叠展示。
 */

import type { OpsChatItem } from "@/hooks/use-ops-chat"

export interface ChatTurnGroup {
  id: string
  userMessage?: Extract<OpsChatItem, { kind: "user" }>
  processItems: OpsChatItem[]
  assistantMessage?: Extract<OpsChatItem, { kind: "assistant" }>
  errors: Extract<OpsChatItem, { kind: "error" }>[]
  /** 批准之后设备还在读、或摘要还没回到对话里 */
  deviceQueryWait: DeviceQueryWait | null
}

/** 批准后、摘要出现前，对话区要一直显示的阶段 */
export type DeviceQueryWait = "reading" | "summarizing"

/**
 * 这一轮是不是还在等设备配置或它的摘要。
 *
 * 审批前那句「已提交审批」会先成为最终回答，执行过程又被折起来，
 * 所以批准之后如果不再显示进行中，看起来就像没回复。
 */
export function deviceQueryWait(items: OpsChatItem[]): DeviceQueryWait | null {
  const summarized = new Set(
    items.flatMap((item) =>
      item.kind === "assistant" &&
      item.source === "device_query_summary" &&
      item.proposalId != null
        ? [item.proposalId]
        : [],
    ),
  )
  let wait: DeviceQueryWait | null = null
  for (const item of items) {
    if (item.kind !== "hitl") continue
    const status = item.status.trim().toUpperCase()
    const executionState = item.executionState ?? null
    if (status === "EXECUTED" && item.actionType === "device_query") {
      const summaryLanded =
        summarized.has(item.proposalId) ||
        items.some(
          (message) =>
            message.kind === "assistant" &&
            isAfter(message.createdAt, item.executedAt),
        )
      wait = summaryLanded ? null : "summarizing"
      continue
    }
    // 开端口这类变更没有配置摘要。执行一旦结束就不能再看旧的排队标记，
    // 否则 WebSocket 只更新了状态、没清掉 executionState，界面会一直停在生成中。
    if (status === "EXECUTED" || status === "REJECTED" || status === "UNKNOWN") {
      wait = null
      continue
    }
    if (
      executionState === "queued" ||
      executionState === "running" ||
      status === "EXECUTING"
    ) {
      wait = "reading"
      continue
    }
    if (
      executionState === "awaiting_credential" ||
      status === "UNKNOWN" ||
      status === "REJECTED"
    ) {
      wait = null
    }
  }
  return wait
}

function isAfter(
  later: string | undefined,
  earlier: string | null | undefined,
): boolean {
  if (!later || !earlier) return false
  const left = Date.parse(later)
  const right = Date.parse(earlier)
  return Number.isFinite(left) && Number.isFinite(right) && left > right
}

/** 进行中气泡的文案 */
export function deviceQueryWaitLabel(wait: DeviceQueryWait): string {
  if (wait === "reading") return "已批准，正在登录设备并读取配置…"
  return "配置已取回，正在生成摘要…"
}

/**
 * 将扁平的时间线条目按问答轮次（Turn）归组
 */
export function groupMessagesIntoTurns(messages: OpsChatItem[]): ChatTurnGroup[] {
  if (messages.length === 0) return []

  // 1. 先按 user 消息将消息流切分为若干个 raw turns
  const rawTurns: {
    id: string
    userMessage?: Extract<OpsChatItem, { kind: "user" }>
    items: OpsChatItem[]
  }[] = []

  let currentRawTurn: {
    id: string
    userMessage?: Extract<OpsChatItem, { kind: "user" }>
    items: OpsChatItem[]
  } | null = null

  for (const item of messages) {
    if (item.kind === "user") {
      currentRawTurn = {
        id: `turn:${item.id}`,
        userMessage: item,
        items: [],
      }
      rawTurns.push(currentRawTurn)
      continue
    }

    if (!currentRawTurn) {
      currentRawTurn = {
        id: `turn:${item.id}`,
        items: [],
      }
      rawTurns.push(currentRawTurn)
    }

    currentRawTurn.items.push(item)
  }

  // 2. 对每个 raw turn 进行处理：最后一个 assistant 作为最终回答，其余全部进入 processItems
  const groups: ChatTurnGroup[] = []

  for (const raw of rawTurns) {
    const processItems: OpsChatItem[] = []
    const errors: Extract<OpsChatItem, { kind: "error" }>[] = []

    // 找到本轮中的最后一个 assistant 索引
    let lastAssistantIdx = -1
    for (let i = raw.items.length - 1; i >= 0; i--) {
      if (raw.items[i].kind === "assistant") {
        lastAssistantIdx = i
        break
      }
    }

    let finalAssistantMessage: Extract<OpsChatItem, { kind: "assistant" }> | undefined

    for (let i = 0; i < raw.items.length; i++) {
      const item = raw.items[i]

      if (item.kind === "error") {
        errors.push(item)
        continue
      }

      if (i === lastAssistantIdx && item.kind === "assistant") {
        finalAssistantMessage = item
      } else {
        // 中间的所有 assistant 文本、tool_call、hitl、child 等均进入 processItems
        processItems.push(item)
      }
    }

    groups.push({
      id: raw.id,
      userMessage: raw.userMessage,
      processItems,
      assistantMessage: finalAssistantMessage,
      errors,
      deviceQueryWait: deviceQueryWait(raw.items),
    })
  }

  return groups
}
