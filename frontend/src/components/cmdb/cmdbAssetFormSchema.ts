/** CMDB 资产表单 zod 校验与凭据字段辅助函数 */

import { z } from "zod"

import type { CredentialType, EnableCredentialType } from "@/types/cmdb"

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
 *   existingEnableType: 资产当前的 enable 口令类型；新建传 null。规则和登录密码
 *     一样：只有本来就是静态时，口令才允许留空（保留原密文）。
 */
export function createFormSchema(
  existingCredentialType: CredentialType | null,
  vendorValues: readonly string[] = [],
  existingEnableType: EnableCredentialType | null = null
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
      enable_credential_type: z
        .enum(["none", "static"])
        .optional()
        .default("none"),
      enable_password: z.string().max(256).optional().default(""),
    })
    .superRefine((data, ctx) => {
      if (data.enable_credential_type === "none" && data.enable_password) {
        ctx.addIssue({
          code: "custom",
          path: ["enable_password"],
          message: "enable 口令类型为「无」时不能填写口令",
        })
      }
      if (
        data.enable_credential_type === "static" &&
        !data.enable_password &&
        existingEnableType !== "static"
      ) {
        ctx.addIssue({
          code: "custom",
          path: ["enable_password"],
          message: "新登记静态 enable 口令时必须填写口令",
        })
      }
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

/**
 * 提交时 enable 口令相关字段怎么带。
 *
 * Args:
 *   data: 表单里的厂商与 enable 字段。
 *   enableVendors: 目录给出的「要 enable 的厂商」；目录还没加载回来时传 null。
 *
 * Returns:
 *   - 目录没加载完：一个字段都不带。此时不知道这个厂商要不要 enable，
 *     贸然按「无」提交会把思科设备已登记的口令清掉；不带字段后端就保留原值。
 *   - 厂商不需要 enable：按「无」提交，顺手清掉换厂商之前留下的口令。
 *   - 需要 enable：带上类型；静态且填了新口令才带口令，留空表示保留原口令。
 */
export function enablePayloadFields(
  data: Pick<
    CmdbAssetFormValues,
    "vendor" | "enable_credential_type" | "enable_password"
  >,
  enableVendors: readonly string[] | null
): { enable_credential_type?: EnableCredentialType; enable_password?: string } {
  if (enableVendors === null) return {}
  if (!enableVendors.includes(data.vendor)) {
    return { enable_credential_type: "none" }
  }
  if (data.enable_credential_type === "static" && data.enable_password) {
    return {
      enable_credential_type: "static",
      enable_password: data.enable_password,
    }
  }
  return { enable_credential_type: data.enable_credential_type }
}
