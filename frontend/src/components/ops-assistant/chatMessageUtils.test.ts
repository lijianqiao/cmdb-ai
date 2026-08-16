/** chatMessageUtils 单元测试：问答轮次归组与只显示最终回答 */

import { describe, expect, it } from "vitest"

import type { OpsChatItem } from "@/hooks/use-ops-chat"
import { groupMessagesIntoTurns } from "./chatMessageUtils"

describe("groupMessagesIntoTurns", () => {
  it("空列表返回空", () => {
    expect(groupMessagesIntoTurns([])).toEqual([])
  })

  it("单轮标准对话：提取唯一 assistant 作为最终回答", () => {
    const messages: OpsChatItem[] = [
      { kind: "user", id: "u:1", content: "查状态" },
      { kind: "tool_call", id: "tc:1", toolCallId: "c1", name: "ping" },
      { kind: "assistant", id: "a:1", content: "设备在线", streaming: false },
    ]

    const turns = groupMessagesIntoTurns(messages)
    expect(turns).toHaveLength(1)
    expect(turns[0].userMessage?.content).toBe("查状态")
    expect(turns[0].processItems).toHaveLength(1)
    expect(turns[0].processItems[0]).toMatchObject({ kind: "tool_call" })
    expect(turns[0].assistantMessage?.content).toBe("设备在线")
  })

  it("一轮中包含多次中间 assistant 文本、工具与审批：仅最后一条 assistant 为最终回答，前面全部收入 processItems", () => {
    const messages: OpsChatItem[] = [
      { kind: "user", id: "u:1", content: "备份交换机配置" },
      // 步骤 1：思考与查资产
      { kind: "assistant", id: "a:1", content: "正在查询 CMDB 设备资产...", streaming: false },
      { kind: "tool_call", id: "tc:1", toolCallId: "c1", name: "cmdb_list" },
      // 步骤 2：发起审批
      { kind: "assistant", id: "a:2", content: "已找到资产，准备执行高危命令，请审批", streaming: false },
      {
        kind: "hitl",
        id: "hitl:101",
        proposalId: 101,
        actionType: "device_backup",
        status: "APPROVED",
        reason: "备份配置",
        assetId: 5,
        resultExcerpt: null,
        hasFullResult: true,
      },
      // 步骤 3：审批后执行
      { kind: "tool_call", id: "tc:2", toolCallId: "c2", name: "run_backup" },
      // 步骤 4：最终回答
      { kind: "assistant", id: "a:3", content: "配置备份成功！文件已归档到 /data/sw1.cfg", streaming: false },
    ]

    const turns = groupMessagesIntoTurns(messages)
    expect(turns).toHaveLength(1)
    expect(turns[0].userMessage?.content).toBe("备份交换机配置")

    // 最终回答必须是最后一条 a:3
    expect(turns[0].assistantMessage?.id).toBe("a:3")
    expect(turns[0].assistantMessage?.content).toBe("配置备份成功！文件已归档到 /data/sw1.cfg")

    // 最终回答之前的所有内容必须进入 processItems
    expect(turns[0].processItems.map((item) => item.id)).toEqual([
      "a:1",
      "tc:1",
      "a:2",
      "hitl:101",
      "tc:2",
    ])
  })

  it("多轮对话各自独立：各自提取最终回答与中间过程", () => {
    const messages: OpsChatItem[] = [
      // 轮次 1
      { kind: "user", id: "u:1", content: "第 1 问" },
      { kind: "assistant", id: "a:1", content: "中间 1", streaming: false },
      { kind: "tool_call", id: "tc:1", toolCallId: "c1", name: "t1" },
      { kind: "assistant", id: "a:2", content: "最终回答 1", streaming: false },
      // 轮次 2
      { kind: "user", id: "u:2", content: "第 2 问" },
      { kind: "tool_call", id: "tc:2", toolCallId: "c2", name: "t2" },
      { kind: "assistant", id: "a:3", content: "最终回答 2", streaming: false },
    ]

    const turns = groupMessagesIntoTurns(messages)
    expect(turns).toHaveLength(2)

    // 轮次 1
    expect(turns[0].userMessage?.content).toBe("第 1 问")
    expect(turns[0].processItems.map((i) => i.id)).toEqual(["a:1", "tc:1"])
    expect(turns[0].assistantMessage?.id).toBe("a:2")

    // 轮次 2
    expect(turns[1].userMessage?.content).toBe("第 2 问")
    expect(turns[1].processItems.map((i) => i.id)).toEqual(["tc:2"])
    expect(turns[1].assistantMessage?.id).toBe("a:3")
  })
})
