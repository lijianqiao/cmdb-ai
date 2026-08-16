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
    })
  }

  return groups
}
