/** 凭据三态切换的字段校验，以及编辑表单提交边界测试 */

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
import type { CmdbAsset } from "@/types/cmdb"

import {
  clearedCredentialFields,
  createFormSchema,
} from "./cmdbAssetFormSchema"
import { CmdbAssetFormDialog } from "./CmdbAssetFormDialog"
import { vendorItems, vendorLabel } from "./cmdbVendors"

// 厂商列表由后端命令目录接口给出：测试里把它 mock 掉，顺带证明前端不再自己写一份。
const CATALOG_VENDORS = [
  "cisco_iosxe",
  "cisco_small_business",
  "huawei_vrp",
  "hp_comware",
  "juniper_junos",
  "other",
]

const baseAssetFields = {
  asset_type: "switch",
  vendor: "huawei_vrp",
  hostname: "sw-01",
  ip_address: "10.0.0.1",
  location: "",
  business_system: "",
  subnet_cidr: "",
  notes: "",
}

// 没开 vitest 全局模式，Testing Library 不会自动卸载：上一条用例的对话框留在页面上时，
// 两个对话框的输入框 id 相同，按标签找到的会是旧对话框里的输入框。
afterEach(cleanup)

beforeEach(() => {
  resetDeviceCommandCatalogCache()
  vi.mocked(api.get).mockReset()
  vi.mocked(api.get).mockResolvedValue({
    data: {
      data: {
        catalog_version: "t17-v1",
        commands: [],
        vendors: CATALOG_VENDORS,
      },
    },
  })
})

describe("CmdbAssetFormDialog 凭据校验规则", () => {
  it("编辑 Small Business 静态凭据资产时保留厂商且不提交空密码", async () => {
    const asset: CmdbAsset = {
      id: 1,
      asset_type: "switch",
      vendor: "cisco_small_business",
      hostname: "lab-switch",
      ip_address: "192.0.2.10",
      location: "测试机房",
      owner_user_id: null,
      business_system: "",
      subnet_cidr: "",
      notes: "",
      credential_type: "static",
      credential_username: "test-admin",
      credential_password_set: true,
      ssh_port: 2222,
      created_at: "2026-08-14T00:00:00Z",
      updated_at: "2026-08-14T00:00:00Z",
    }
    const onSubmit = vi.fn().mockResolvedValue(true)

    render(
      <CmdbAssetFormDialog
        open
        onOpenChange={vi.fn()}
        asset={asset}
        onSubmit={onSubmit}
      />
    )

    fireEvent.click(screen.getByRole("button", { name: "确定" }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    const payload = onSubmit.mock.calls[0]?.[0]
    expect(payload).toHaveProperty("vendor", "cisco_small_business")
    expect(payload).not.toHaveProperty("credential_password")
    // 编辑时原样带回登记的端口，而且是数字，不是输入框里的字符串。
    expect(payload).toHaveProperty("ssh_port", 2222)
  })

  it("新增资产不填端口时按 22 提交", async () => {
    const onSubmit = vi.fn().mockResolvedValue(true)

    render(
      <CmdbAssetFormDialog
        open
        onOpenChange={vi.fn()}
        asset={null}
        onSubmit={onSubmit}
      />
    )
    fireEvent.change(screen.getByLabelText("主机名"), {
      target: { value: "sw-port" },
    })
    fireEvent.change(screen.getByLabelText("IP 地址"), {
      target: { value: "10.0.0.9" },
    })
    fireEvent.click(screen.getByRole("button", { name: "确定" }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    expect(onSubmit.mock.calls[0]?.[0]).toHaveProperty("ssh_port", 22)
  })

  it("SSH 端口只接受 1–65535 的整数", () => {
    const schema = createFormSchema(null)
    const withPort = (ssh_port: string) =>
      schema.safeParse({
        ...baseAssetFields,
        ssh_port,
        credential_type: "none",
        ...clearedCredentialFields(),
      }).success

    expect(withPort("2222")).toBe(true)
    expect(withPort("0")).toBe(false)
    expect(withPort("65536")).toBe(false)
    expect(withPort("22a")).toBe(false)
    expect(withPort("")).toBe(false)
  })

  it("厂商下拉项来自目录返回的厂商值，标签只在前端补", () => {
    expect(vendorItems(CATALOG_VENDORS)).toContainEqual({
      label: "H3C / HP Comware",
      value: "hp_comware",
    })
    expect(vendorItems(CATALOG_VENDORS)).toContainEqual({
      label: "其他 / 未指定",
      value: "other",
    })
    // 后端新接一个厂商、前端还没补标签时，原样显示厂商值，不会少一个选项。
    expect(vendorLabel("ruijie_os")).toBe("ruijie_os")
  })

  it("表单只接受目录返回的厂商；目录还没加载回来时不拦合法提交", () => {
    const withCatalog = createFormSchema(null, CATALOG_VENDORS)
    expect(
      withCatalog.safeParse({
        ...baseAssetFields,
        vendor: "linux",
        credential_type: "none",
        ...clearedCredentialFields(),
      }).success
    ).toBe(false)
    expect(
      withCatalog.safeParse({
        ...baseAssetFields,
        credential_type: "none",
        ...clearedCredentialFields(),
      }).success
    ).toBe(true)
    // 目录为空（还没加载回来）时只要求非空：取值由后端再校验一次。
    expect(
      createFormSchema(null, []).safeParse({
        ...baseAssetFields,
        vendor: "linux",
        credential_type: "none",
        ...clearedCredentialFields(),
      }).success
    ).toBe(true)
  })

  it("表单拒绝服务器等非网络资产类型", () => {
    const result = createFormSchema(null).safeParse({
      ...baseAssetFields,
      asset_type: "server",
      credential_type: "none",
      ...clearedCredentialFields(),
    })
    expect(result.success).toBe(false)
  })

  it("新增资产默认是交换机、厂商未指定", async () => {
    const onSubmit = vi.fn().mockResolvedValue(true)

    render(
      <CmdbAssetFormDialog
        open
        onOpenChange={vi.fn()}
        asset={null}
        onSubmit={onSubmit}
      />
    )
    fireEvent.change(screen.getByLabelText("主机名"), {
      target: { value: "sw-new" },
    })
    fireEvent.change(screen.getByLabelText("IP 地址"), {
      target: { value: "10.0.0.8" },
    })
    fireEvent.click(screen.getByRole("button", { name: "确定" }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    expect(onSubmit.mock.calls[0]?.[0]).toMatchObject({
      asset_type: "switch",
      vendor: "other",
    })
  })

  it("显示 SG350X 对应的 Cisco Small Business 厂商选项", () => {
    expect(vendorItems(CATALOG_VENDORS)).toContainEqual({
      label: "思科 Small Business（SG350X 等）",
      value: "cisco_small_business",
    })
  })

  it("允许选择 Cisco Small Business CLI 平台", () => {
    const result = createFormSchema(null).safeParse({
      ...baseAssetFields,
      vendor: "cisco_small_business",
      credential_type: "none",
      ...clearedCredentialFields(),
    })
    expect(result.success).toBe(true)
  })

  it("none 类型不允许账号或密码", () => {
    const result = createFormSchema(null).safeParse({
      ...baseAssetFields,
      credential_type: "none",
      credential_username: "admin",
    })
    expect(result.success).toBe(false)
  })

  it("none 类型在凭据字段已清空时应通过", () => {
    const result = createFormSchema(null).safeParse({
      ...baseAssetFields,
      credential_type: "none",
      ...clearedCredentialFields(),
    })
    expect(result.success).toBe(true)
  })

  it("static 类型必须有账号", () => {
    const result = createFormSchema(null).safeParse({
      ...baseAssetFields,
      credential_type: "static",
    })
    expect(result.success).toBe(false)
  })

  it("新建 static 类型必须有密码", () => {
    const result = createFormSchema(null).safeParse({
      ...baseAssetFields,
      credential_type: "static",
      credential_username: "admin",
    })
    expect(result.success).toBe(false)
  })

  it("新建 static 类型账号密码齐全时应通过", () => {
    const result = createFormSchema(null).safeParse({
      ...baseAssetFields,
      credential_type: "static",
      credential_username: "admin",
      credential_password: "p@ss",
    })
    expect(result.success).toBe(true)
  })

  it("编辑本来就是 static 的资产，密码留空应通过（保留原密文）", () => {
    const result = createFormSchema("static").safeParse({
      ...baseAssetFields,
      credential_type: "static",
      credential_username: "admin",
      credential_password: "",
    })
    expect(result.success).toBe(true)
  })

  it("编辑时从 none/dynamic 切换为 static，密码留空应拦截", () => {
    for (const existingType of ["none", "dynamic"] as const) {
      const result = createFormSchema(existingType).safeParse({
        ...baseAssetFields,
        credential_type: "static",
        credential_username: "admin",
        credential_password: "",
      })
      expect(result.success).toBe(false)
    }
  })

  it("编辑时从 none/dynamic 切换为 static 并填写密码应通过", () => {
    const result = createFormSchema("dynamic").safeParse({
      ...baseAssetFields,
      credential_type: "static",
      credential_username: "admin",
      credential_password: "new-pwd",
    })
    expect(result.success).toBe(true)
  })

  it("dynamic 类型允许只填账号", () => {
    const result = createFormSchema(null).safeParse({
      ...baseAssetFields,
      credential_type: "dynamic",
      credential_username: "otp-admin",
    })
    expect(result.success).toBe(true)
  })

  it("dynamic 类型不允许填密码", () => {
    const result = createFormSchema(null).safeParse({
      ...baseAssetFields,
      credential_type: "dynamic",
      credential_username: "otp-admin",
      credential_password: "nope",
    })
    expect(result.success).toBe(false)
  })

  it("切换凭据类型时 clearedCredentialFields 清空残留值", () => {
    expect(clearedCredentialFields()).toEqual({
      credential_username: "",
      credential_password: "",
    })
  })
})
