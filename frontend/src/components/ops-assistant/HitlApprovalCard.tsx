/** HITL 审批卡片

 * WS 只带安全摘要；有 agent:hitl_approve 时再 HTTP 拉完整 payload。
 * 批准/拒绝走 /hitl/proposals/{id}/decide；拒绝二次确认。
 * device_control stub 失败后状态停留 APPROVED → 展示「已批准但未执行」。
 */

import { useEffect, useState } from "react"
import { isAxiosError } from "axios"
import { toast } from "sonner"

import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Spinner } from "@/components/ui/spinner"
import { usePermission } from "@/hooks/use-permission"
import { Cancel01Icon, Tick02Icon } from "@/lib/icons"
import {
  decideHitlProposal,
  getHitlProposal,
  type HitlProposal,
} from "@/lib/hitl-api"
import { PERMISSIONS } from "@/lib/constants"
import { cn } from "@/lib/utils"

export interface HitlApprovalCardProps {
  proposalId: number
  actionType: string
  status: string
  reason: string
  assetId: number | null
  className?: string
}

/**
 * 将后端/WS 状态码映射为中文展示文案。
 *
 * Args:
 *   status: 提案状态（大小写不敏感）
 *
 * Returns:
 *   中文状态标签
 */
function statusLabel(status: string): string {
  switch (status.trim().toUpperCase()) {
    case "PENDING":
      return "等待审批"
    case "REJECTED":
      return "已拒绝"
    case "EXECUTED":
      return "已执行"
    case "APPROVED":
    case "EXECUTION_FAILED":
      // T10：device_control stub 失败保持 APPROVED，前端统一为「已批准但未执行」
      return "已批准但未执行"
    default:
      return status || "未知状态"
  }
}

/**
 * 读取接口错误中的中文 message/detail。
 *
 * Args:
 *   error: 捕获的异常
 *   fallback: 默认文案
 *
 * Returns:
 *   可展示的错误字符串
 */
function readErrorMessage(error: unknown, fallback: string): string {
  if (!isAxiosError(error)) return fallback
  const data = error.response?.data
  if (data && typeof data === "object") {
    const message = (data as { message?: unknown }).message
    if (typeof message === "string" && message.trim()) return message
    const detail = (data as { detail?: unknown }).detail
    if (typeof detail === "string" && detail.trim()) return detail
  }
  return fallback
}

/**
 * 从完整提案中提取展示用 reason / asset_id（与安全摘要同源）。
 *
 * Args:
 *   proposal: HTTP 详情
 *
 * Returns:
 *   reason 与 assetId
 */
function readPayloadMeta(proposal: HitlProposal): {
  reason: string
  assetId: number | null
} {
  const payload = proposal.action_payload
  const rawReason = payload.proposal_reason
  const reason = typeof rawReason === "string" ? rawReason : ""
  const rawAsset = payload.asset_id
  const assetId =
    typeof rawAsset === "number" && Number.isInteger(rawAsset) ? rawAsset : null
  return { reason, assetId }
}

/**
 * HITL 审批卡片：权限门控拉取完整载荷，并提供批准/拒绝。
 *
 * Args:
 *   proposalId / actionType / status / reason / assetId: WS 安全摘要字段
 *   className: 外层 Card class
 */
export function HitlApprovalCard({
  proposalId,
  actionType,
  status,
  reason,
  assetId,
  className,
}: HitlApprovalCardProps) {
  const { hasPermission } = usePermission()
  const canApprove = hasPermission(PERMISSIONS.AGENT_HITL_APPROVE)

  const [detail, setDetail] = useState<HitlProposal | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [deciding, setDeciding] = useState(false)
  const [rejectOpen, setRejectOpen] = useState(false)
  const [localStatus, setLocalStatus] = useState<string | null>(null)

  const displayStatus = localStatus ?? status
  const normalized = displayStatus.trim().toUpperCase()
  const isPending = normalized === "PENDING" || normalized === ""

  const displayActionType = detail?.action_type || actionType
  const meta = detail ? readPayloadMeta(detail) : null
  const displayReason = meta?.reason || reason
  const displayAssetId = meta?.assetId ?? assetId

  useEffect(() => {
    setLocalStatus(null)
    setDetail(null)
    setDetailError(null)
  }, [proposalId])

  useEffect(() => {
    if (localStatus != null && status.trim().toUpperCase() !== "PENDING") {
      setLocalStatus(null)
    }
  }, [status, localStatus])

  useEffect(() => {
    if (!canApprove) return

    let cancelled = false
    setDetailLoading(true)
    setDetailError(null)

    void getHitlProposal(proposalId)
      .then((proposal) => {
        if (!cancelled) setDetail(proposal)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        const message = readErrorMessage(error, "加载审批详情失败")
        setDetailError(message)
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [canApprove, proposalId])

  const handleApprove = async (): Promise<void> => {
    if (!canApprove || !isPending || deciding) return
    setDeciding(true)
    try {
      const updated = await decideHitlProposal(proposalId, { approve: true })
      setDetail(updated)
      setLocalStatus(updated.status)
      toast.success(
        updated.status.trim().toUpperCase() === "APPROVED" && !updated.executed_at
          ? "已批准但未执行"
          : "审批完成",
      )
    } catch (error: unknown) {
      toast.error(readErrorMessage(error, "批准失败"))
    } finally {
      setDeciding(false)
    }
  }

  const handleRejectConfirm = async (): Promise<boolean> => {
    if (!canApprove || !isPending || deciding) return false
    setDeciding(true)
    try {
      const updated = await decideHitlProposal(proposalId, { approve: false })
      setDetail(updated)
      setLocalStatus(updated.status)
      toast.success("已拒绝该提案")
      return true
    } catch (error: unknown) {
      toast.error(readErrorMessage(error, "拒绝失败"))
      return false
    } finally {
      setDeciding(false)
    }
  }

  return (
    <>
      <Card className={cn("bg-card", className)}>
        <CardHeader className="gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={isPending ? "outline" : "secondary"}>
              {statusLabel(displayStatus)}
            </Badge>
            {displayActionType ? (
              <Badge variant="secondary">{displayActionType}</Badge>
            ) : null}
            <span className="text-xs text-muted-foreground">
              #{proposalId}
            </span>
          </div>
          <CardTitle className="text-base">人工审批</CardTitle>
          <CardDescription>
            {canApprove
              ? "完整动作载荷仅对审批人可见。"
              : "等待具备审批权限的同事处理；此处仅显示安全摘要。"}
          </CardDescription>
        </CardHeader>

        <CardContent className="flex flex-col gap-2">
          {displayReason ? (
            <p className="text-sm text-foreground">{displayReason}</p>
          ) : (
            <p className="text-sm text-muted-foreground">无审批说明</p>
          )}
          {displayAssetId != null ? (
            <p className="text-xs text-muted-foreground">资产 #{displayAssetId}</p>
          ) : null}

          {canApprove ? (
            detailLoading ? (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <Spinner className="size-3" />
                <span>加载完整载荷…</span>
              </div>
            ) : detailError ? (
              <p className="text-xs text-destructive">{detailError}</p>
            ) : detail ? (
              <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 text-xs text-muted-foreground whitespace-pre-wrap break-words">
                {JSON.stringify(detail.action_payload, null, 2)}
              </pre>
            ) : null
          ) : null}
        </CardContent>

        {canApprove && isPending ? (
          <CardFooter className="flex flex-wrap gap-2">
            <Button
              type="button"
              size="sm"
              disabled={deciding || detailLoading}
              onClick={() => void handleApprove()}
            >
              {deciding ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <Tick02Icon data-icon="inline-start" />
              )}
              批准
            </Button>
            <Button
              type="button"
              size="sm"
              variant="destructive"
              disabled={deciding || detailLoading}
              onClick={() => setRejectOpen(true)}
            >
              <Cancel01Icon data-icon="inline-start" />
              拒绝
            </Button>
          </CardFooter>
        ) : null}
      </Card>

      <ConfirmDialog
        open={rejectOpen}
        onOpenChange={setRejectOpen}
        title="确认拒绝提案"
        description="拒绝后该提案不可再批准，确定要继续吗？"
        confirmText="确认拒绝"
        cancelText="取消"
        variant="destructive"
        onConfirm={handleRejectConfirm}
      />
    </>
  )
}
