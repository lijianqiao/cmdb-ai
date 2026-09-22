/** 设备命令目录 hook：只读、全站共用，缓存一次；失败不缓存。 */

// @vitest-environment jsdom

import { cleanup, renderHook, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({ default: { get: vi.fn() } }))

import api from "@/lib/api"

import {
  resetDeviceCommandCatalogCache,
  useDeviceCommandCatalog,
} from "./use-device-command-catalog"

const CATALOG = {
  catalog_version: "t17-v1",
  commands: [
    {
      name: "show_version",
      description: "查看设备版本信息",
      command_type: "read_only",
      arguments: [],
      vendors: ["cisco_iosxe"],
    },
    {
      name: "port_disable",
      description: "禁用一组网络接口",
      command_type: "state_changing",
      arguments: ["interface_names"],
      vendors: ["cisco_iosxe", "hp_comware"],
    },
  ],
  vendors: ["cisco_iosxe", "hp_comware", "other"],
}

describe("useDeviceCommandCatalog", () => {
  beforeEach(() => {
    resetDeviceCommandCatalogCache()
    vi.mocked(api.get).mockReset()
    vi.mocked(api.get).mockResolvedValue({ data: { data: CATALOG } })
  })

  afterEach(cleanup)

  it("拉回命令与厂商，并缓存给其它页面复用", async () => {
    const first = renderHook(() => useDeviceCommandCatalog())

    await waitFor(() =>
      expect(first.result.current.catalog.vendors).toEqual([
        "cisco_iosxe",
        "hp_comware",
        "other",
      ])
    )
    expect(first.result.current.loading).toBe(false)

    renderHook(() => useDeviceCommandCatalog())
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1))
  })

  it("请求失败不缓存：下次挂载还会再试一次", async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error("offline"))

    const failed = renderHook(() => useDeviceCommandCatalog())
    await waitFor(() => expect(failed.result.current.loading).toBe(false))
    expect(failed.result.current.catalog.commands).toEqual([])

    renderHook(() => useDeviceCommandCatalog())
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
  })
})
