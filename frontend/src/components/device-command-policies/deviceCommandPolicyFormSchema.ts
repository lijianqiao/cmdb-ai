/** 设备命令策略创建表单的校验规则。
 *
 * 单独成文件的原因：规则要接收后端命令目录给的「变更类命令名」，成了一个导出的
 * 函数；和组件放同一个文件里会破坏 React 的热更新（只导出组件才能热替换）。
 */

import { z } from "zod"

/** 构造创建表单的校验规则。
 *
 * Args:
 *   stateChangingCommands: 会改变设备状态的命令名，来自后端命令目录。目录还没
 *     加载回来时是空集：这条规则先不生效，后端仍会拒绝（422），不会放松边界。
 */
export function createPolicySchema(stateChangingCommands: ReadonlySet<string>) {
  return z
    .object({
      scope: z.enum(["asset_type", "asset"]),
      asset_type: z.string().optional(),
      asset_id: z.string().optional(),
      command_name: z.string().min(1, "请选择命令"),
      decision: z.enum(["whitelist", "blacklist"]),
      note: z.string().max(500).optional().default(""),
    })
    .superRefine((data, ctx) => {
      if (data.scope === "asset_type") {
        if (!data.asset_type) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "请选择设备类型",
            path: ["asset_type"],
          })
        }
      } else if (!data.asset_id || !/^\d+$/.test(data.asset_id)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "请选择 CMDB 资产",
          path: ["asset_id"],
        })
      }
      if (
        stateChangingCommands.has(data.command_name) &&
        data.scope !== "asset"
      ) {
        ctx.addIssue({
          code: "custom",
          path: ["scope"],
          message: "变更类命令只能按单台设备配置",
        })
      }
    })
}

export type CreateFormData = z.infer<ReturnType<typeof createPolicySchema>>
