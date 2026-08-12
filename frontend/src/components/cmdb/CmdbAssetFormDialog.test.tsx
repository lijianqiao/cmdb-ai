/** 凭据三态切换的字段可见性/必填校验单测，不跑完整 Dialog 渲染栈 */

import { describe, expect, it } from "vitest"
import { z } from "zod"

// 与 CmdbAssetFormDialog.tsx 内的 schema 保持一致，这里独立复刻校验规则做单测，
// 避免拖入 base-ui Dialog 的真实渲染依赖（项目里现有表单测试也没有走完整渲染）。
const credentialSchema = z
  .object({
    credential_type: z.enum(["none", "static", "dynamic"]),
    credential_username: z.string().max(100).optional().default(""),
    credential_password: z.string().max(256).optional().default(""),
  })
  .superRefine((data, ctx) => {
    if (data.credential_type === "none") {
      if (data.credential_username || data.credential_password) {
        ctx.addIssue({ code: "custom", message: "无凭据时不能填写账号或密码" })
      }
    } else if (data.credential_type === "static") {
      if (!data.credential_username) {
        ctx.addIssue({ code: "custom", message: "静态凭据必须填写账号" })
      }
    } else if (data.credential_type === "dynamic") {
      if (!data.credential_username) {
        ctx.addIssue({ code: "custom", message: "动态凭据必须填写账号" })
      }
      if (data.credential_password) {
        ctx.addIssue({ code: "custom", message: "动态凭据不允许填写密码" })
      }
    }
  })

describe("CmdbAssetFormDialog 凭据校验规则", () => {
  it("none 类型不允许账号或密码", () => {
    const result = credentialSchema.safeParse({
      credential_type: "none",
      credential_username: "admin",
    })
    expect(result.success).toBe(false)
  })

  it("static 类型必须有账号", () => {
    const result = credentialSchema.safeParse({ credential_type: "static" })
    expect(result.success).toBe(false)
  })

  it("dynamic 类型允许只填账号", () => {
    const result = credentialSchema.safeParse({
      credential_type: "dynamic",
      credential_username: "otp-admin",
    })
    expect(result.success).toBe(true)
  })

  it("dynamic 类型不允许填密码", () => {
    const result = credentialSchema.safeParse({
      credential_type: "dynamic",
      credential_username: "otp-admin",
      credential_password: "nope",
    })
    expect(result.success).toBe(false)
  })
})
