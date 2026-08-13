/** 监控目标表单校验单测 */

import { describe, expect, it } from "vitest"

import { monitorTargetFormSchema } from "./monitorTargetFormSchema"

describe("monitorTargetFormSchema", () => {
  it("接受合法的 IP、端口与可选资产 ID", () => {
    const parsed = monitorTargetFormSchema.parse({
      ip_address: "10.0.0.5",
      port: "22",
      label: "SSH",
      check_interval_seconds: "30",
      is_active: true,
      cmdb_asset_id: "12",
    })
    expect(parsed.port).toBe(22)
    expect(parsed.cmdb_asset_id).toBe("12")
  })

  it("允许不填 CMDB 资产 ID", () => {
    const parsed = monitorTargetFormSchema.parse({
      ip_address: "10.0.0.5",
      port: 22,
      label: "",
      check_interval_seconds: 30,
      is_active: true,
      cmdb_asset_id: "",
    })
    expect(parsed.cmdb_asset_id).toBe("")
  })

  it("拒绝非法端口", () => {
    const result = monitorTargetFormSchema.safeParse({
      ip_address: "10.0.0.5",
      port: 0,
      label: "",
      check_interval_seconds: 30,
      is_active: true,
      cmdb_asset_id: "",
    })
    expect(result.success).toBe(false)
  })
})
