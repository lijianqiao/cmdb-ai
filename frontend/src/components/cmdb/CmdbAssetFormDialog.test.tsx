/** 凭据三态切换的字段可见性/必填校验单测，不跑完整 Dialog 渲染栈 */

import { describe, expect, it } from "vitest"

import {
  clearedCredentialFields,
  createFormSchema,
} from "./cmdbAssetFormSchema"

const baseAssetFields = {
  asset_type: "server",
  vendor: "generic",
  hostname: "srv-01",
  ip_address: "10.0.0.1",
  location: "",
  business_system: "",
  subnet_cidr: "",
  notes: "",
}

describe("CmdbAssetFormDialog 凭据校验规则", () => {
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
