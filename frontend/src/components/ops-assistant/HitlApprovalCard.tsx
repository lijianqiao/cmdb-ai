/** HITL 审批时间线卡片
 *
 * 在时间线中呈现审批状态、执行结果与完整配置抽屉；
 * 支持动态凭据 InputOTP 输入与审批决策/重试/处置操作。
 */

import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  InputOTP,
  InputOTPGroup,
  InputOTPSlot,
} from "@/components/ui/input-otp"
import { Label } from "@/components/ui/label"
import { Spinner } from "@/components/ui/spinner"
import { Cancel01Icon, Shield02Icon, Tick02Icon } from "@/lib/icons"
import { usePermission } from "@/hooks/use-permission"
import {
  decideHitlProposal,
  getHitlProposal,
  resolveUnknownHitlProposal,
  retryHitlProposal,
  type HitlProposal,
} from "@/lib/hitl-api"
import { PERMISSIONS } from "@/lib/constants"
import {
  getDeviceQueryResult,
  recoverDeviceQuerySummary,
} from "@/lib/agent-api"
import { cn } from "@/lib/utils"
import type { DeviceQueryResult } from "@/types/agent"
import {
  isApproveButtonDisabled,
  isRetryAvailable,
  isUnknownResolutionAvailable,
  needsDynamicCredentialPassword,
  readErrorMessage,
  readLastError,
  readPayloadMeta,
  shouldShowResultExcerpt,
  statusLabel,
} from "@/components/ops-assistant/hitlApprovalCardUtils"

export interface HitlApprovalCardProps {
  sessionId: number
  proposalId: number
  actionType: string
  status: string
  reason: string
  assetId: number | null
  /** WS 安全摘要可能携带的执行结果片段（无审批权限时展示用） */
  resultExcerpt?: string | null
  hasFullResult: boolean
  className?: string
}

export function HitlApprovalCard({
  sessionId,
  proposalId,
  actionType,
  status,
  reason,
  assetId,
  resultExcerpt,
  hasFullResult,
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
  const [dynamicPassword, setDynamicPassword] = useState("")
  const [fullResultOpen, setFullResultOpen] = useState(false)
  const [fullResult, setFullResult] = useState<DeviceQueryResult | null>(null)
  const [fullResultLoading, setFullResultLoading] = useState(false)
  const [fullResultError, setFullResultError] = useState<string | null>(null)
  const [summaryRecovering, setSummaryRecovering] = useState(false)
  const fullResultRequestRef = useRef(0)

  const displayStatus = localStatus ?? status
  const normalized = displayStatus.trim().toUpperCase()
  const isPending = normalized === "PENDING" || normalized === ""

  const displayActionType = detail?.action_type || actionType
  const meta = detail ? readPayloadMeta(detail) : null
  const displayReason = meta?.reason || reason
  const displayAssetId = meta?.assetId ?? assetId

  const needsDynamicPassword = needsDynamicCredentialPassword(
    displayActionType,
    detail?.asset_credential_type,
  )

  const resolvedResultExcerpt = detail?.result_excerpt ?? resultExcerpt ?? null
  const showResultExcerpt = shouldShowResultExcerpt(
    displayStatus,
    resolvedResultExcerpt,
  )

  const lastError = readLastError(detail?.action_payload)
  const retryAvailable = isRetryAvailable(canApprove, displayStatus)
  const unknownResolutionAvailable = isUnknownResolutionAvailable(
    canApprove,
    displayStatus,
  )
  const showFullResultArea =
    normalized === "EXECUTED" &&
    displayActionType.trim().toLowerCase() === "device_query"

  const approveDisabled = isApproveButtonDisabled(
    deciding,
    detailLoading,
    needsDynamicPassword,
    dynamicPassword,
  )

  useEffect(() => {
    setLocalStatus(null)
    setDetail(null)
    setDetailLoading(false)
    setDetailError(null)
    setDynamicPassword("")
    setDeciding(false)
    setRejectOpen(false)

    fullResultRequestRef.current += 1
    setFullResultOpen(false)
    setFullResult(null)
    setFullResultLoading(false)
    setFullResultError(null)
    setSummaryRecovering(false)

    if (!canApprove) return

    let cancelled = false
    setDetailLoading(true)
    setDetailError(null)

    void getHitlProposal(proposalId)
      .then((proposal) => {
        if (!cancelled) setDetail(proposal)
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setDetailError(readErrorMessage(error, "加载审批详情失败"))
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [canApprove, sessionId, proposalId])

  useEffect(() => {
    if (localStatus != null && status.trim().toUpperCase() !== "PENDING") {
      setLocalStatus(null)
    }
  }, [status, localStatus])

  const handleApprove = async (): Promise<void> => {
    if (!canApprove || !isPending || deciding) return
    const passwordToUse = dynamicPassword.trim()
    if (needsDynamicPassword && !passwordToUse) return

    setDeciding(true)
    try {
      const body: { approve: true; dynamic_credential_password?: string } = {
        approve: true,
      }
      if (needsDynamicPassword) {
        body.dynamic_credential_password = passwordToUse
      }
      const updated = await decideHitlProposal(proposalId, body)
      setDetail(updated)
      setLocalStatus(updated.status)
      setDynamicPassword("")
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

  const handleRetry = async (): Promise<void> => {
    if (!retryAvailable || deciding) return
    const passwordToUse = dynamicPassword.trim()
    setDeciding(true)
    try {
      const body: { dynamic_credential_password?: string } = {}
      if (needsDynamicPassword) {
        body.dynamic_credential_password = passwordToUse
      }
      const updated = await retryHitlProposal(proposalId, body)
      setDetail(updated)
      setLocalStatus(updated.status)
      setDynamicPassword("")
      toast.success(
        updated.status.trim().toUpperCase() === "EXECUTED"
          ? "重试执行成功"
          : "重试后仍未执行成功，请检查设备连通性与凭据",
      )
    } catch (error: unknown) {
      toast.error(readErrorMessage(error, "重试失败"))
    } finally {
      setDeciding(false)
    }
  }

  const handleResolveUnknown = async (
    resolution: "confirm_executed" | "allow_retry",
  ): Promise<void> => {
    if (!unknownResolutionAvailable || deciding) return
    setDeciding(true)
    try {
      const updated = await resolveUnknownHitlProposal(proposalId, resolution)
      setDetail(updated)
      setLocalStatus(updated.status)
      setDynamicPassword("")
      toast.success(
        resolution === "confirm_executed"
          ? "已确认设备上命令已执行"
          : "已允许重试，请再次执行",
      )
    } catch (error: unknown) {
      toast.error(readErrorMessage(error, "处置失败"))
    } finally {
      setDeciding(false)
    }
  }

  const loadFullResult = async (): Promise<void> => {
    if (fullResult != null || fullResultLoading) return
    const requestId = fullResultRequestRef.current + 1
    fullResultRequestRef.current = requestId
    setFullResultLoading(true)
    setFullResultError(null)
    try {
      const result = await getDeviceQueryResult(sessionId, proposalId)
      if (fullResultRequestRef.current === requestId) setFullResult(result)
    } catch (error: unknown) {
      if (fullResultRequestRef.current === requestId) {
        setFullResultError(readErrorMessage(error, "加载完整配置失败"))
      }
    } finally {
      if (fullResultRequestRef.current === requestId) {
        setFullResultLoading(false)
      }
    }
  }

  const handleFullResultOpenChange = (open: boolean): void => {
    setFullResultOpen(open)
    if (open && fullResult == null && !fullResultLoading) {
      void loadFullResult()
    }
  }

  const handleRecoverSummary = async (): Promise<void> => {
    if (fullResult?.summary_status !== "pending" || summaryRecovering) return
    const requestId = fullResultRequestRef.current
    setSummaryRecovering(true)
    setFullResultError(null)
    try {
      const result = await recoverDeviceQuerySummary(sessionId, proposalId)
      if (fullResultRequestRef.current === requestId) {
        setFullResult((current) =>
          current == null
            ? current
            : { ...current, summary_status: result.summary_status },
        )
      }
    } catch (error: unknown) {
      if (fullResultRequestRef.current === requestId) {
        setFullResultError(readErrorMessage(error, "恢复 AI 总结失败"))
      }
    } finally {
      if (fullResultRequestRef.current === requestId) {
        setSummaryRecovering(false)
      }
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
        onConfirm={handleRejectConfirm}
      />

      <Card className={cn("overflow-hidden border border-border/70 bg-card", className)}>
        <CardHeader className="gap-2 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <Shield02Icon className="size-4 text-primary" />
              <CardTitle className="text-base font-semibold">人工审批</CardTitle>
              <Badge variant={isPending ? "default" : "secondary"}>
                {statusLabel(displayStatus)}
              </Badge>
              {displayActionType ? (
                <Badge variant="outline">{displayActionType}</Badge>
              ) : null}
              <span className="text-xs text-muted-foreground">
                #{proposalId}
              </span>
            </div>
          </div>
          <CardDescription>
            {canApprove
              ? "完整动作载荷仅对审批人可见。"
              : "等待具备审批权限的同事处理；此处仅显示安全摘要。"}
          </CardDescription>
        </CardHeader>

        <CardContent className="flex flex-col gap-2 p-4 pt-0">
          {displayReason ? (
            <p className="text-sm text-foreground">{displayReason}</p>
          ) : (
            <p className="text-sm text-muted-foreground">无审批说明</p>
          )}
          {displayAssetId != null ? (
            <p className="text-xs text-muted-foreground">资产 #{displayAssetId}</p>
          ) : null}

          {lastError && normalized === "APPROVED" ? (
            <p className="text-xs text-destructive" data-testid="hitl-last-error">
              上次执行失败：{lastError}
            </p>
          ) : null}

          {showResultExcerpt ? (
            <div className="flex flex-col gap-1">
              <p className="text-xs font-medium text-muted-foreground">执行结果</p>
              <pre
                className="max-h-48 overflow-auto rounded-md bg-muted p-2 text-xs text-muted-foreground whitespace-pre-wrap break-words"
                data-testid="hitl-result-excerpt"
              >
                {resolvedResultExcerpt}
              </pre>
            </div>
          ) : null}

          {showFullResultArea ? (
            hasFullResult ? (
              <Collapsible
                open={fullResultOpen}
                onOpenChange={handleFullResultOpenChange}
                className="flex flex-col gap-2 border-t border-border/40 pt-2"
              >
                <CollapsibleTrigger
                  render={<Button type="button" size="xs" variant="outline" />}
                >
                  {fullResultOpen ? "收起完整配置" : "查看完整配置"}
                </CollapsibleTrigger>
                <CollapsibleContent className="flex flex-col gap-2">
                  {fullResultLoading ? (
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <Spinner className="size-3" />
                      <span>加载完整配置…</span>
                    </div>
                  ) : fullResultError ? (
                    <div className="flex flex-col items-start gap-2">
                      <p className="text-xs text-destructive">
                        {fullResultError}
                      </p>
                      {fullResult == null ? (
                        <Button
                          type="button"
                          size="xs"
                          variant="outline"
                          onClick={() => void loadFullResult()}
                        >
                          重试加载
                        </Button>
                      ) : null}
                    </div>
                  ) : null}
                  {fullResult ? (
                    <div className="flex flex-col gap-2">
                      <p className="text-xs text-muted-foreground">
                        {fullResult.content_length} 个字符
                      </p>
                      <pre
                        className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 text-xs text-muted-foreground"
                        data-testid="hitl-full-result"
                      >
                        {fullResult.content}
                      </pre>
                      {fullResult.summary_status === "pending" ? (
                        <Button
                          type="button"
                          size="xs"
                          variant="outline"
                          disabled={summaryRecovering}
                          onClick={() => void handleRecoverSummary()}
                        >
                          {summaryRecovering ? (
                            <Spinner data-icon="inline-start" />
                          ) : null}
                          恢复 AI 总结
                        </Button>
                      ) : null}
                      {fullResult.summary_status === "generating" ? (
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          <Spinner className="size-3" />
                          <span>AI 总结生成中</span>
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </CollapsibleContent>
              </Collapsible>
            ) : (
              <p className="text-xs text-muted-foreground">
                该历史记录仅保存了预览，无法恢复完整配置。
              </p>
            )
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
          <CardFooter className="flex flex-col items-stretch gap-3 border-t border-border/40 p-4 sm:flex-row sm:flex-wrap sm:items-end">
            {needsDynamicPassword ? (
              <div className="flex min-w-[200px] flex-1 flex-col gap-1.5">
                <Label htmlFor={`hitl-dynamic-password-${proposalId}`}>
                  动态凭据密码 (OTP)
                </Label>
                <InputOTP
                  id={`hitl-dynamic-password-${proposalId}`}
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
              </div>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                disabled={approveDisabled}
                onClick={() => void handleApprove()}
                data-testid="hitl-approve-button"
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
            </div>
          </CardFooter>
        ) : null}

        {retryAvailable ? (
          <CardFooter className="flex flex-col items-stretch gap-3 border-t border-border/40 p-4 sm:flex-row sm:flex-wrap sm:items-end">
            {needsDynamicPassword ? (
              <div className="flex min-w-[200px] flex-1 flex-col gap-1.5">
                <Label htmlFor={`hitl-retry-password-${proposalId}`}>
                  重试动态凭据密码 (OTP)
                </Label>
                <InputOTP
                  id={`hitl-retry-password-${proposalId}`}
                  maxLength={6}
                  value={dynamicPassword}
                  onChange={(val) => setDynamicPassword(val)}
                  disabled={deciding}
                  data-testid="hitl-retry-password"
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
              </div>
            ) : null}
            <Button
              type="button"
              size="sm"
              disabled={
                deciding ||
                detailLoading ||
                (needsDynamicPassword && !dynamicPassword.trim())
              }
              onClick={() => void handleRetry()}
              data-testid="hitl-retry-button"
            >
              {deciding ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <Tick02Icon data-icon="inline-start" />
              )}
              重试执行
            </Button>
          </CardFooter>
        ) : null}

        {unknownResolutionAvailable ? (
          <CardFooter className="flex flex-wrap gap-2 border-t border-border/40 p-4">
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={deciding}
              onClick={() => void handleResolveUnknown("confirm_executed")}
              data-testid="hitl-confirm-executed-button"
            >
              确认已执行
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={deciding}
              onClick={() => void handleResolveUnknown("allow_retry")}
              data-testid="hitl-allow-retry-button"
            >
              允许重试
            </Button>
          </CardFooter>
        ) : null}
      </Card>
    </>
  )
}
