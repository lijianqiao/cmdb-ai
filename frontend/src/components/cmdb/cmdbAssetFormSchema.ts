/** CMDB 资产表单 zod 校验与凭据字段辅助函数 */

import { z } from "zod"

import type { CredentialType, VendorName } from "@/types/cmdb"

/** 厂商枚举值须与后端 app/agent/device_commands.py::VendorName 手动保持一致，后端才是权威来源 */
const VENDOR_VALUES = [
  "cisco_iosxe",
  "huawei_vrp",
  "hp_comware",
  "juniper_junos",
  "linux",
  "generic",
] as const satisfies readonly VendorName[]

/** 切换凭据类型时返回应写入 RHF 的空凭据字段，避免隐藏字段残留导致 zod 失败 */
export function clearedCredentialFields(): {
  credential_username: string
  credential_password: string
} {
  return { credential_username: "", credential_password: "" }
}

/**
 * 构造表单校验 schema。
 *
 * Args:
 *   existingCredentialType: 资产当前持久化的凭据类型；新建资产传 null。
 *     只有「本来就是 static」时才允许密码留空（后端会保留原密文）——
 *     新建，或者从 none/dynamic 切换成 static，都必须填新密码，否则
 *     不存在可保留的旧密文，留空会被后端 422 拒绝，这里提前拦截。
 */
export function createFormSchema(existingCredentialType: CredentialType | null) {
  return z
    .object({
      asset_type: z.string().min(1, "请选择资产类型").max(50),
      vendor: z.enum(VENDOR_VALUES, { message: "请选择厂商" }),
      hostname: z.string().min(1, "请输入主机名").max(255),
      ip_address: z.string().min(1, "请输入 IP 地址").max(45),
      location: z.string().max(200).optional().default(""),
      business_system: z.string().max(100).optional().default(""),
      subnet_cidr: z.string().max(45).optional().default(""),
      notes: z.string().max(2000).optional().default(""),
      credential_type: z.enum(["none", "static", "dynamic"]),
      credential_username: z.string().max(100).optional().default(""),
      credential_password: z.string().max(256).optional().default(""),
    })
    .superRefine((data, ctx) => {
      if (data.credential_type === "none") {
        if (data.credential_username || data.credential_password) {
          ctx.addIssue({
            code: "custom",
            path: ["credential_username"],
            message: "凭据类型为「无」时不能填写账号或密码",
          })
        }
      } else if (data.credential_type === "static") {
        if (!data.credential_username) {
          ctx.addIssue({
            code: "custom",
            path: ["credential_username"],
            message: "静态凭据必须填写账号",
          })
        }
        const passwordCanBeOmitted = existingCredentialType === "static"
        if (!passwordCanBeOmitted && !data.credential_password) {
          ctx.addIssue({
            code: "custom",
            path: ["credential_password"],
            message:
              existingCredentialType === null
                ? "新建静态凭据必须填写密码"
                : "从其他凭据类型切换为静态密码时必须填写新密码",
          })
        }
      } else if (data.credential_type === "dynamic") {
        if (!data.credential_username) {
          ctx.addIssue({
            code: "custom",
            path: ["credential_username"],
            message: "动态凭据必须填写账号",
          })
        }
        if (data.credential_password) {
          ctx.addIssue({
            code: "custom",
            path: ["credential_password"],
            message: "动态凭据不需要也不允许填写密码",
          })
        }
      }
    })
}

export type CmdbAssetFormValues = z.infer<ReturnType<typeof createFormSchema>>
