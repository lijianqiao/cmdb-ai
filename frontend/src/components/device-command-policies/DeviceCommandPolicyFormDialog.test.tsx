/** 命令策略表单：命令清单和「变更类只能按单台设备」都来自后端命令目录。 */

// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({ default: { get: vi.fn() } }))

import api from "@/lib/api"
import { resetDeviceCommandCatalogCache } from "@/hooks/use-device-command-catalog"

import { DeviceCommandPolicyFormDialog } from "./DeviceCommandPolicyFormDialog"
import { createPolicySchema } from "./deviceCommandPolicyFormSchema"

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
      vendors: ["cisco_iosxe"],
    },
  ],
  vendors: ["cisco_iosxe", "other"],
}

afterEach(cleanup)

beforeEach(() => {
  resetDeviceCommandCatalogCache()
  vi.mocked(api.get).mockReset()
  vi.mocked(api.get).mockResolvedValue({ data: { data: CATALOG } })
})

describe("createPolicySchema", () => {
  it("变更类命令只能按单台设备配置", () => {
    const schema = createPolicySchema(new Set(["port_disable"]))

    expect(
      schema.safeParse({
        scope: "asset_type",
        asset_type: "switch",
        command_name: "port_disable",
        decision: "whitelist",
      }).success
    ).toBe(false)
    expect(
      schema.safeParse({
        scope: "asset",
        asset_id: "7",
        command_name: "port_disable",
        decision: "whitelist",
      }).success
    ).toBe(true)
    expect(
      schema.safeParse({
        scope: "asset_type",
        asset_type: "switch",
        command_name: "show_version",
        decision: "whitelist",
      }).success
    ).toBe(true)
  })

  it("目录还没加载回来时这条规则先不生效，由后端拒绝", () => {
    const schema = createPolicySchema(new Set<string>())

    expect(
      schema.safeParse({
        scope: "asset_type",
        asset_type: "switch",
        command_name: "port_disable",
        decision: "whitelist",
      }).success
    ).toBe(true)
  })
})

describe("DeviceCommandPolicyFormDialog", () => {
  it("命令默认值取目录返回的第一条命令", async () => {
    const onSubmit = vi.fn().mockResolvedValue(true)

    render(
      <DeviceCommandPolicyFormDialog
        open
        onOpenChange={vi.fn()}
        policy={null}
        onSubmit={onSubmit}
      />
    )
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    fireEvent.click(screen.getByRole("button", { name: "确定" }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    expect(onSubmit.mock.calls[0]?.[0]).toMatchObject({
      scope: "asset_type",
      asset_type: "switch",
      command_name: "show_version",
    })
  })
})
