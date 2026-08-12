/** CMDB 资产表单 zod 校验与凭据字段辅助函数 */

import { z } from "zod"

/** 切换凭据类型时返回应写入 RHF 的空凭据字段，避免隐藏字段残留导致 zod 失败 */
export function clearedCredentialFields(): {
  credential_username: string
  credential_password: string
} {
  return { credential_username: "", credential_password: "" }
}

export function createFormSchema(isEdit: boolean) {
  return z
    .object({
      asset_type: z.string().min(1, "请选择资产类型").max(50),
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
        if (!isEdit && !data.credential_password) {
          ctx.addIssue({
            code: "custom",
            path: ["credential_password"],
            message: "新建静态凭据必须填写密码",
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
