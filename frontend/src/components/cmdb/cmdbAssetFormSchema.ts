/** CMDB 资产表单 zod 校验与凭据字段辅助函数 */

import { z } from "zod"

import type { CredentialType } from "@/types/cmdb"

import { isAssetTypeName } from "./cmdbAssetTypes"

function isValidSshPort(value: string): boolean {
  if (!/^\d{1,5}$/.test(value)) return false
  const port = Number(value)
  return port >= 1 && port <= 65535
}

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
 *   vendorValues: 后端命令目录给出的厂商值。目录还没加载回来时传空数组：
 *     只要求非空，不校验取值——否则表单会因为「目录还没到」而拦下合法的提交，
 *     取值本身后端还会再校验一次。
 */
export function createFormSchema(
  existingCredentialType: CredentialType | null,
  vendorValues: readonly string[] = []
) {
  const vendor =
    vendorValues.length > 0
      ? z.string().refine((value) => vendorValues.includes(value), "请选择厂商")
      : z.string().min(1, "请选择厂商")
  return z
    .object({
      // 字段值保持 string：编辑清理前的旧资产（如 server）时要把旧值原样显示出来，
      // 提交时才拦下，让人自己改成网络设备类型，而不是悄悄换掉。
      asset_type: z.string().refine(isAssetTypeName, "请选择资产类型"),
      vendor,
      hostname: z.string().min(1, "请输入主机名").max(255),
      ip_address: z.string().min(1, "请输入 IP 地址").max(45),
      location: z.string().max(200).optional().default(""),
      business_system: z.string().max(100).optional().default(""),
      subnet_cidr: z.string().max(45).optional().default(""),
      notes: z.string().max(2000).optional().default(""),
      // 输入框拿到的是字符串，提交时再转成数字；不填按 22。
      ssh_port: z
        .string()
        .refine(isValidSshPort, "SSH 端口必须是 1–65535 的整数")
        .optional()
        .default("22"),
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
