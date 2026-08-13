/** 监控目标表单 zod 校验 */

import { z } from "zod"

export const monitorTargetFormSchema = z.object({
  ip_address: z.string().min(1, "请输入 IP 地址").max(45, "IP 地址过长"),
  port: z.coerce
    .number()
    .int("端口必须是整数")
    .min(1, "端口不能小于 1")
    .max(65535, "端口不能大于 65535"),
  label: z.string().max(100, "标签不能超过 100 个字符").default(""),
  check_interval_seconds: z.coerce
    .number()
    .int("巡检间隔必须是整数")
    .min(5, "巡检间隔不能小于 5 秒")
    .max(3600, "巡检间隔不能超过 3600 秒"),
  is_active: z.boolean(),
  cmdb_asset_id: z
    .string()
    .refine(
      (value) => value === "" || /^\d+$/.test(value),
      "CMDB 资产 ID 必须是正整数",
    ),
})

export type MonitorTargetFormValues = z.infer<typeof monitorTargetFormSchema>
