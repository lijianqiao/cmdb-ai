/** 一轮回答下方的用量小字：输入/输出 token 与花费。
 *
 * 显示的是**整轮**合计——一次提问会跑多步循环，还可能派生子 Agent，
 * 这里给的是这些加起来的总数，不是最后一次模型调用的用量。
 *
 * 不显示「系统 token」：OpenAI 兼容接口返回的 prompt_tokens 本身就已经包含
 * 系统提示词，服务端不把它单列，硬拆只能靠本地估算，估出来的数还跟总数对不上。
 */

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { TurnUsage } from "@/types/agent"
import { CHAT_TIER_LABELS, type ChatTier } from "@/types/system-config"

const TOKEN_FORMAT = new Intl.NumberFormat("zh-CN")

/** 花费按量级选精度：$0.0021 这种小数额保留 4 位才看得出差别 */
function formatCost(costUsd: number): string {
  if (costUsd === 0) return "$0"
  if (costUsd < 0.01) return `$${costUsd.toFixed(4)}`
  return `$${costUsd.toFixed(2)}`
}

/** 模型登记键 → 档位中文名；认不出的键原样显示，不猜 */
function modelLabel(modelKey: string): string {
  const tier = modelKey.startsWith("chat-") ? modelKey.slice("chat-".length) : null
  if (tier && tier in CHAT_TIER_LABELS) {
    return CHAT_TIER_LABELS[tier as ChatTier]
  }
  return modelKey
}

export interface TurnUsageLineProps {
  usage: TurnUsage
}

export function TurnUsageLine({ usage }: TurnUsageLineProps) {
  const total = usage.promptTokens + usage.completionTokens
  const byModel = Object.entries(usage.byModel ?? {})

  const summary = (
    <span className="cursor-default text-xs text-muted-foreground tabular-nums">
      输入 {TOKEN_FORMAT.format(usage.promptTokens)} · 输出{" "}
      {TOKEN_FORMAT.format(usage.completionTokens)} · 合计{" "}
      {TOKEN_FORMAT.format(total)} tokens · {formatCost(usage.costUsd)}
    </span>
  )

  // 只有一个模型时明细跟汇总是同一份数字，弹出来纯属噪音
  if (byModel.length < 2) {
    return <div className="mt-2 border-t pt-2">{summary}</div>
  }

  return (
    <div className="mt-2 border-t pt-2">
      <Tooltip>
        <TooltipTrigger render={summary} />
        <TooltipContent>
          <ul className="space-y-0.5 text-xs tabular-nums">
            {byModel.map(([model, item]) => (
              <li key={model}>
                {modelLabel(model)}：{TOKEN_FORMAT.format(item.prompt_tokens)} /{" "}
                {TOKEN_FORMAT.format(item.completion_tokens)} ·{" "}
                {formatCost(item.cost_usd)}
              </li>
            ))}
          </ul>
        </TooltipContent>
      </Tooltip>
    </div>
  )
}
