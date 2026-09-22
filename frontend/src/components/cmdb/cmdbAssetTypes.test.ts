/** CMDB 资产类型常量：只登记网络设备，旧数据的类型原样显示 */

import { describe, expect, it } from "vitest"

import {
  ASSET_TYPE_ITEMS,
  ASSET_TYPE_VALUES,
  assetTypeLabel,
  isAssetTypeName,
} from "./cmdbAssetTypes"

describe("cmdbAssetTypes", () => {
  it("只有网络设备类型，没有服务器、负载均衡、存储", () => {
    expect(ASSET_TYPE_VALUES).toEqual([
      "switch",
      "router",
      "firewall",
      "wireless_controller",
      "other",
    ])
    expect(ASSET_TYPE_ITEMS.map((item) => item.value)).toEqual([
      ...ASSET_TYPE_VALUES,
    ])
    expect(isAssetTypeName("wireless_controller")).toBe(true)
    expect(isAssetTypeName("server")).toBe(false)
  })

  it("按值取中文标签", () => {
    expect(assetTypeLabel("switch")).toBe("交换机")
    expect(assetTypeLabel("wireless_controller")).toBe("无线控制器")
    expect(assetTypeLabel("other")).toBe("其他网络设备")
  })

  it("清理前的旧类型原样显示，不能显示成空白", () => {
    expect(assetTypeLabel("server")).toBe("server")
  })
})
