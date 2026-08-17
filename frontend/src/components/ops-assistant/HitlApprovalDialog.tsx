/** HITL 审批模态对话框
 *
 * 弹出展示审批详情、Shadcn InputOTP 密码录入与操作按钮。
 * 用户点击批准并执行或确认拒绝时，弹窗立即关闭消失，不阻塞界面。
 */

import { useEffect, useState } from "react"
import { toast } from "sonner"

import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  InputOTP,
  InputOTPGroup,
  InputOTPSlot,
} from "@/components/ui/input-otp"
import { Label } from "@/components/ui/label"
import { Spinner } from "@/components/ui/spinner"
import { usePermission } from "@/hooks/use-permission"
import { Cancel01Icon, Tick02Icon } from "@/lib/icons"
import {
  decideHitlProposal,
  getHitlProposal,
  type HitlProposal,
} from "@/lib/hitl-api"
import { PERMISSIONS } from "@/lib/constants"
import {
  isApproveButtonDisabled,
  isRetryAvailable,
  isUnknownResolutionAvailable,
  needsDynamicCredentialPassword,
  readErrorMessage,
  readLastError,
  shouldShowResultExcerpt,
  statusLabel,
} from "@/components/ops-assistant/hitlApprovalCardUtils"

export interface HitlApprovalDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  sessionId: number
  proposalId: number
  actionType: string
  status: string
  reason: string
  assetId: number | null
  resultExcerpt?: string | null
  detail?: HitlProposal | null
  detailLoading?: boolean
  detailError?: string | null
  onApprove?: (dynamicPassword: string) => Promise<void> | void
  onReject?: () => Promise<boolean | void> | boolean | void
  onRetry?: (dynamicPassword: string) => Promise<void> | void
  onResolveUnknown?: (resolution: "confirm_executed" | "allow_retry") => Promise<void> | void
  deciding?: boolean
  lastError?: string | null
}

export function HitlApprovalDialog({
  open,
  onOpenChange,
  sessionId: _sessionId,
  proposalId,
  actionType,
  status,
  reason,
  assetId,
  resultExcerpt,
  detail: propDetail,
  detailLoading: propDetailLoading,
  detailError: propDetailError,
  onApprove,
  onReject,
  onRetry,
  onResolveUnknown,
  deciding: propDeciding,
  lastError: propLastError,
}: HitlApprovalDialogProps) {
  const { hasPermission } = usePermission()
  const canApprove = hasPermission(PERMISSIONS.AGENT_HITL_APPROVE)

  const [innerDetail, setInnerDetail] = useState<HitlProposal | null>(null)
  const [innerLoading, setInnerLoading] = useState(false)
  const [innerError, setInnerError] = useState<string | null>(null)
  const [innerDeciding, setInnerDeciding] = useState(false)
  const [rejectOpen, setRejectOpen] = useState(false)
  const [dynamicPassword, setDynamicPassword] = useState("")

  const detail = propDetail !== undefined ? propDetail : innerDetail
  const detailLoading = propDetailLoading !== undefined ? propDetailLoading : innerLoading
  const detailError = propDetailError !== undefined ? propDetailError : innerError
  const deciding = propDeciding !== undefined ? propDeciding : innerDeciding

  // 状态与 detail 遵循同一优先级：拉到的详情比 prop 新。
  // 「审批成功但执行失败」时后端返回 200 + status=APPROVED，但**不会**发
  // hitl_resolved 事件（批准路径不发），所以 prop 里的 status 还停在 PENDING。
  // 不在这里取 detail 的话，弹窗会一直显示「批准」按钮，用户再点又因状态已变
  // 拿到 409，卡死循环。
  const effectiveStatus = detail?.status ?? status
  const normalized = effectiveStatus.trim().toUpperCase()
  const isPending = normalized === "PENDING" || normalized === ""

  const displayActionType = detail?.action_type || actionType
  const needsDynamicPassword = needsDynamicCredentialPassword(
    displayActionType,
    detail?.asset_credential_type,
  )

  const resolvedResultExcerpt = detail?.result_excerpt ?? resultExcerpt ?? null
  const showResultExcerpt = shouldShowResultExcerpt(
    effectiveStatus,
    resolvedResultExcerpt,
  )

  const lastError = propLastError ?? readLastError(detail?.action_payload)
  const retryAvailable = isRetryAvailable(canApprove, effectiveStatus)
  const unknownResolutionAvailable = isUnknownResolutionAvailable(
    canApprove,
    effectiveStatus,
  )

  const approveDisabled = isApproveButtonDisabled(
    deciding,
    detailLoading,
    needsDynamicPassword,
    dynamicPassword,
  )

  useEffect(() => {
    if (!open) {
      setDynamicPassword("")
      setRejectOpen(false)
      return
    }

    // 若父级未传 detail 且有权限，则内部自动获取
    if (propDetail === undefined && canApprove) {
      let cancelled = false
      setInnerLoading(true)
      setInnerError(null)
      void getHitlProposal(proposalId)
        .then((p) => {
          if (!cancelled) setInnerDetail(p)
        })
        .catch((err: unknown) => {
          if (!cancelled) setInnerError(readErrorMessage(err, "加载审批详情失败"))
        })
        .finally(() => {
          if (!cancelled) setInnerLoading(false)
        })
      return () => {
        cancelled = true
      }
    }
  }, [open, propDetail, canApprove, proposalId])

  const handleApproveClick = () => {
    if (!canApprove || !isPending || deciding || approveDisabled) return
    const pwd = dynamicPassword.trim()
    if (needsDynamicPassword && !pwd) return

    // 关键优化：点击批准的第一时间立即关闭弹窗并清空密码，绝不阻塞等待回答输出
    setDynamicPassword("")
    onOpenChange(false)

    if (onApprove) {
      void onApprove(pwd)
      return
    }

    setInnerDeciding(true)
    void (async () => {
      try {
        const body: { approve: true; dynamic_credential_password?: string } = {
          approve: true,
        }
        if (needsDynamicPassword) {
          body.dynamic_credential_password = pwd
        }
        const updated = await decideHitlProposal(proposalId, body)
        setInnerDetail(updated)
        if (updated.execution_error) {
          // 审批本身成功了，失败的是执行——说清楚是哪一步，并提示可重试，
          // 否则用户会以为要重新批准一次（而那只会拿到状态冲突）。
          toast.warning(`已批准，但执行未启动：${updated.execution_error}。可重试执行`)
        } else if (
          updated.status.trim().toUpperCase() === "APPROVED" &&
          !updated.executed_at
        ) {
          toast.success("已批准但未执行")
        } else {
          toast.success("审批完成，正在执行...")
        }
      } catch (error: unknown) {
        toast.error(readErrorMessage(error, "批准失败"))
        // 真正的失败也要把最新状态拉回来：审批可能已经落库（部分成功），
        // 不刷新的话弹窗会停在 PENDING，用户重复点击只会一直拿到冲突。
        void getHitlProposal(proposalId)
          .then(setInnerDetail)
          .catch(() => undefined)
      } finally {
        setInnerDeciding(false)
      }
    })()
  }

  const handleRejectClick = async (): Promise<boolean> => {
    if (!canApprove || !isPending || deciding) return false

    // 关键优化：第一时间立即关闭弹窗
    onOpenChange(false)
    setRejectOpen(false)

    if (onReject) {
      void onReject()
      return true
    }

    setInnerDeciding(true)
    void (async () => {
      try {
        const updated = await decideHitlProposal(proposalId, { approve: false })
        setInnerDetail(updated)
        toast.success("已拒绝该提案")
      } catch (error: unknown) {
        toast.error(readErrorMessage(error, "拒绝失败"))
      } finally {
        setInnerDeciding(false)
      }
    })()
    return true
  }

  const handleRetryClick = () => {
    const pwd = dynamicPassword.trim()
    setDynamicPassword("")
    onOpenChange(false)
    if (onRetry) {
      void onRetry(pwd)
    }
  }

  return (
    <>
      <ConfirmDialog
        open={rejectOpen}
        onOpenChange={setRejectOpen}
        title="确认拒绝该提案"
        description="拒绝后当前命令或变更将不会在设备上执行，该轮次将以拒绝结果继续。"
        confirmText="确认拒绝"
        cancelText="取消"
        variant="destructive"
        onConfirm={handleRejectClick}
      />

      <Dialog open={open} onOpenChange={onOpenChange} modal={false}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={isPending ? "default" : "secondary"}>
                {statusLabel(status)}
              </Badge>
              {displayActionType ? (
                <Badge variant="outline">{displayActionType}</Badge>
              ) : null}
              <span className="text-xs text-muted-foreground">
                提案 #{proposalId}
              </span>
            </div>
            <DialogTitle className="text-base font-semibold">
              人工审批请求
            </DialogTitle>
            <DialogDescription>
              {canApprove
                ? "请审查本次运维操作载荷与风险，确认无误后批准执行。"
                : "当前账户无审批权限，仅展示安全摘要。"}
            </DialogDescription>
          </DialogHeader>

          <div className="flex flex-col gap-3 py-1">
            {/* 审批说明 */}
            <div className="rounded-lg bg-muted/60 p-3">
              <p className="text-xs font-medium text-muted-foreground">审批说明</p>
              <p className="mt-1 text-sm text-foreground">
                {reason || "无审批说明"}
              </p>
              {assetId != null ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  关联资产 ID: #{assetId}
                </p>
              ) : null}
            </div>

            {lastError && normalized === "APPROVED" ? (
              <p className="text-xs text-destructive" data-testid="hitl-last-error">
                上次执行失败：{lastError}
              </p>
            ) : null}

            {/* 载荷预览 */}
            {canApprove ? (
              detailLoading ? (
                <div className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
                  <Spinner className="size-3.5" />
                  <span>加载动作详情载荷...</span>
                </div>
              ) : detailError ? (
                <p className="text-xs text-destructive">{detailError}</p>
              ) : detail ? (
                <div>
                  <p className="mb-1 text-xs font-medium text-muted-foreground">
                    动作载荷 (Payload)
                  </p>
                  <pre className="max-h-40 overflow-auto rounded-lg bg-muted p-2.5 text-xs text-muted-foreground whitespace-pre-wrap break-words">
                    {JSON.stringify(detail.action_payload, null, 2)}
                  </pre>
                </div>
              ) : null
            ) : null}

            {/* 执行结果片段 */}
            {showResultExcerpt ? (
              <div>
                <p className="mb-1 text-xs font-medium text-muted-foreground">
                  执行结果
                </p>
                <pre
                  className="max-h-36 overflow-auto rounded-lg bg-muted p-2.5 text-xs text-muted-foreground whitespace-pre-wrap break-words"
                  data-testid="hitl-result-excerpt"
                >
                  {resolvedResultExcerpt}
                </pre>
              </div>
            ) : null}

            {/* 动态凭据密码（使用 Shadcn InputOTP 组件） */}
            {canApprove && (isPending || retryAvailable) && needsDynamicPassword ? (
              <div className="flex flex-col items-center gap-2 rounded-lg border border-border/70 bg-card p-3">
                <Label
                  htmlFor={`hitl-otp-dialog-${proposalId}`}
                  className="text-xs font-medium text-foreground"
                >
                  请输入动态凭据密码 (OTP)
                </Label>
                <InputOTP
                  id={`hitl-otp-dialog-${proposalId}`}
                  maxLength={6}
                  value={dynamicPassword}
                  onChange={(val) => setDynamicPassword(val)}
                  disabled={deciding}
                  data-testid="hitl-dynamic-password"
                >
                  <InputOTPGroup>
                    <InputOTPSlot index={0} />
                    <InputOTPSlot index={1} />
                    <InputOTPSlot index={2} />
                    <InputOTPSlot index={3} />
                    <InputOTPSlot index={4} />
                    <InputOTPSlot index={5} />
                  </InputOTPGroup>
                </InputOTP>
                <p className="text-[11px] text-muted-foreground">
                  批准时输入本次登录口令，凭据不会落库
                </p>
              </div>
            ) : null}
          </div>

          <DialogFooter className="gap-2 sm:gap-2">
            {canApprove && isPending ? (
              <>
                <Button
                  type="button"
                  variant="destructive"
                  disabled={deciding || detailLoading}
                  onClick={() => setRejectOpen(true)}
                >
                  <Cancel01Icon data-icon="inline-start" />
                  拒绝
                </Button>
                <Button
                  type="button"
                  disabled={approveDisabled}
                  onClick={handleApproveClick}
                  data-testid="hitl-approve-button"
                >
                  {deciding ? (
                    <Spinner data-icon="inline-start" />
                  ) : (
                    <Tick02Icon data-icon="inline-start" />
                  )}
                  批准并执行
                </Button>
              </>
            ) : null}

            {retryAvailable ? (
              <Button
                type="button"
                disabled={
                  deciding ||
                  detailLoading ||
                  (needsDynamicPassword && !dynamicPassword.trim())
                }
                onClick={handleRetryClick}
                data-testid="hitl-retry-button"
              >
                {deciding ? (
                  <Spinner data-icon="inline-start" />
                ) : (
                  <Tick02Icon data-icon="inline-start" />
                )}
                重试执行
              </Button>
            ) : null}

            {unknownResolutionAvailable ? (
              <>
                <Button
                  type="button"
                  variant="outline"
                  disabled={deciding}
                  onClick={() => {
                    onOpenChange(false)
                    void onResolveUnknown?.("confirm_executed")
                  }}
                  data-testid="hitl-confirm-executed-button"
                >
                  确认已执行
                </Button>
                <Button
                  type="button"
                  disabled={deciding}
                  onClick={() => {
                    onOpenChange(false)
                    void onResolveUnknown?.("allow_retry")
                  }}
                  data-testid="hitl-allow-retry-button"
                >
                  允许重试
                </Button>
              </>
            ) : null}

            {!isPending && !retryAvailable && !unknownResolutionAvailable ? (
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
              >
                关闭
              </Button>
            ) : null}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
