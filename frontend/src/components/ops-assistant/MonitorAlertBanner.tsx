/** 监控告警横幅

 * 展示 useOpsChat 的 monitorAlert（WS monitor_alert 事件）；可关闭。
 */

import { Alert02Icon, Cancel01Icon } from "@/lib/icons"
import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Button } from "@/components/ui/button"

export interface MonitorAlertBannerProps {
  alert: Record<string, unknown> | null
  onDismiss: () => void
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
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          onClick={onDismiss}
          aria-label="关闭告警"
        >
          <Cancel01Icon />
        </Button>
      </AlertAction>
    </Alert>
  )
}
