/** 监控告警横幅

 * 展示 useOpsChat 的 monitorAlert（WS monitor_alert 事件）；可关闭。
 * 「排查」按钮把告警字段拼成一句结构化请求直接发给运维助手，
 * 省掉用户自己切到输入框、回忆 IP 和端口、再手打一遍的过程。
 */

import { Alert02Icon, Cancel01Icon, Search01Icon } from "@/lib/icons"
import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { buildInvestigationPrompt } from "./monitorAlertPrompt"

export interface MonitorAlertBannerProps {
  alert: Record<string, unknown> | null
  onDismiss: () => void
  /** 点击「排查」时收到预填好的排查请求文本；省略则不显示该按钮 */
  onInvestigate?: (prompt: string) => void
  /** 会话忙碌时禁用「排查」，避免与进行中的 turn 冲突 */
  investigateDisabled?: boolean
}

function readText(value: unknown): string {
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value)
  }
  return ""
}

/**
 * 监控告警 Alert；无 alert 时不渲染。
 *
 * Args:
 *   alert: WS payload 字典
 *   onDismiss: 关闭横幅
 */
export function MonitorAlertBanner({
  alert,
  onDismiss,
  onInvestigate,
  investigateDisabled = false,
}: MonitorAlertBannerProps) {
  if (alert == null) return null

  const title =
    readText(alert.title) ||
    readText(alert.name) ||
    readText(alert.alert_name) ||
    "监控告警"
  const message =
    readText(alert.message) ||
    readText(alert.summary) ||
    readText(alert.description) ||
    "收到一条监控告警事件"
  const severity = readText(alert.severity) || readText(alert.level)
  const asset =
    readText(alert.asset_name) ||
    (alert.asset_id != null ? `资产 #${String(alert.asset_id)}` : "")

  return (
    <Alert className="bg-muted">
      <Alert02Icon />
      <AlertTitle>
        {title}
        {severity ? ` · ${severity}` : ""}
      </AlertTitle>
      <AlertDescription>
        <span className="flex flex-col gap-1">
          <span>{message}</span>
          {asset ? (
            <span className="text-xs text-muted-foreground">{asset}</span>
          ) : null}
        </span>
      </AlertDescription>
      <AlertAction>
        <span className="flex items-center gap-1">
          {onInvestigate ? (
            <Button
              type="button"
              variant="outline"
              size="xs"
              disabled={investigateDisabled}
              onClick={() => {
                onInvestigate(buildInvestigationPrompt(alert))
                onDismiss()
              }}
            >
              <Search01Icon />
              排查
            </Button>
          ) : null}
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            onClick={onDismiss}
            aria-label="关闭告警"
          >
            <Cancel01Icon />
          </Button>
        </span>
      </AlertAction>
    </Alert>
  )
}
