/** buildInvestigationPrompt 单测
 *
 * 这个函数是「告警一键排查」的全部逻辑：把 WS payload 里零散的字段拼成
 * 一句给运维助手的请求。测试盯两件事——已知事实必须原样带过去（否则
 * 助手还得反问用户 IP 是多少，等于没省事），字段缺失时不能拼出残句。
 */

import { describe, expect, it } from "vitest"

import { buildInvestigationPrompt } from "./monitorAlertPrompt"

describe("buildInvestigationPrompt", () => {
  it("把告警的已知事实全部带进请求", () => {
    const prompt = buildInvestigationPrompt({
      asset_name: "核心交换机-A",
      ip_address: "10.0.0.1",
      port: 22,
      previous_status: "up",
      status: "down",
      checked_at: "2026-08-17T10:00:00+00:00",
      target_id: 7,
      asset_id: 42,
    })

    expect(prompt).toContain("核心交换机-A")
    expect(prompt).toContain("10.0.0.1:22")
    expect(prompt).toContain("up → down")
    expect(prompt).toContain("2026-08-17T10:00:00+00:00")
    expect(prompt).toContain("监控目标 ID：7")
    expect(prompt).toContain("CMDB 资产 ID：42")
    // 三个默认排查方向要在文案里，助手才知道该并行查什么
    expect(prompt).toContain("监控历史")
    expect(prompt).toContain("依赖")
    expect(prompt).toContain("同网段")
  })

  it("字段缺失时不产生空标签或残句", () => {
    const prompt = buildInvestigationPrompt({ status: "down" })

    expect(prompt).toContain("状态：down")
    expect(prompt).not.toContain("名称：")
    expect(prompt).not.toContain("地址：")
    expect(prompt).not.toContain("undefined")
    expect(prompt).not.toContain("null")
  })

  it("payload 为空对象时仍是一句可用的请求", () => {
    const prompt = buildInvestigationPrompt({})

    expect(prompt).toContain("请排查这条监控告警的原因")
    expect(prompt).not.toContain("undefined")
  })

  it("只有 status 没有 previous_status 时不拼箭头", () => {
    const prompt = buildInvestigationPrompt({ status: "up" })

    expect(prompt).toContain("状态：up")
    expect(prompt).not.toContain("→")
  })

  it("端口缺失时地址不带冒号", () => {
    const prompt = buildInvestigationPrompt({ ip_address: "10.0.0.2" })

    expect(prompt).toContain("地址：10.0.0.2")
    expect(prompt).not.toContain("10.0.0.2:")
  })
})
