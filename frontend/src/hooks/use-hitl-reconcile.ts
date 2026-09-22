/** HITL 审批结果核对 hook

 * 批准/重试接口会同步等设备执行完才返回，慢命令很容易超过浏览器的 30 秒等待。
 * 浏览器放弃等待不等于服务端失败：审批可能已落库，设备可能正在执行或已执行完。
 * 所以请求没拿到明确答复时，只做一件事——按提案 ID 查数据库里的真实状态：
 *
 * 1. 立即查一次，之后按 1s、2s、4s、8s、15s… 退避，总时长有上限；
 * 2. 查到 PENDING（审批可能还在路上）或 EXECUTING（设备还在跑）就接着查，其它状态即落定；
 * 3. 查询本身失败（仍在断网）不算结论，按节奏接着查；
 * 4. 组件卸载或切换提案时中止，不再发请求、不再更新界面；
 * 5. 绝不自动重新批准或重试——重放一条设备变更只能由人决定。
 */

import { useCallback, useEffect, useRef } from "react"

import { getHitlProposal, type HitlProposal } from "@/lib/hitl-api"

/** 第 n 次查询前的等待（毫秒）；超出部分沿用最后一档 */
const RECONCILE_DELAYS_MS = [0, 1_000, 2_000, 4_000, 8_000, 15_000]

/**
 * 核对总时长：浏览器已经等了 30 秒，后端设备连接 15 秒 + 命令读取 60 秒
 * 最多还剩 45 秒，再留出排队余量。到点仍在执行就交给 WS/快照后续更新。
 */
const RECONCILE_BUDGET_MS = 90_000

/** 可被中止的等待：中止时立刻结束，不必等到时间到 */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (ms <= 0 || signal.aborted) {
      resolve()
      return
    }
    const finish = () => {
      clearTimeout(timer)
      signal.removeEventListener("abort", finish)
      resolve()
    }
    const timer = setTimeout(finish, ms)
    signal.addEventListener("abort", finish, { once: true })
  })
}

/** PENDING、EXECUTING，以及仍在排队/执行的请求都不算落定 */
function isSettled(proposal: HitlProposal): boolean {
  if (
    proposal.execution_state === "queued" ||
    proposal.execution_state === "running"
  ) {
    return false
  }
  const normalized = proposal.status.trim().toUpperCase()
  return normalized !== "" && normalized !== "PENDING" && normalized !== "EXECUTING"
}

/**
 * 按提案 ID 有上限地退避查询，直到状态落定。
 *
 * Args:
 *   proposalId: 提案 ID
 *   signal: 中止信号（组件卸载/切换提案）
 *   onUpdate: 每查到一次就回调，便于界面显示「执行中」
 *
 * Returns:
 *   最后一次查到的提案；一次都没查到返回 null
 */
export async function pollHitlProposalUntilSettled(
  proposalId: number,
  {
    signal,
    onUpdate,
  }: { signal: AbortSignal; onUpdate?: (proposal: HitlProposal) => void },
): Promise<HitlProposal | null> {
  const deadline = Date.now() + RECONCILE_BUDGET_MS
  let latest: HitlProposal | null = null
  for (let attempt = 0; ; attempt += 1) {
    const delay =
      RECONCILE_DELAYS_MS[Math.min(attempt, RECONCILE_DELAYS_MS.length - 1)]
    if (Date.now() + delay > deadline) return latest
    await sleep(delay, signal)
    if (signal.aborted) return latest

    let fetched: HitlProposal
    try {
      fetched = await getHitlProposal(proposalId)
    } catch {
      continue
    }
    if (signal.aborted) return latest
    latest = fetched
    onUpdate?.(fetched)
    if (isSettled(fetched)) return fetched
  }
}

/** 一次核对的结论 */
export interface ReconcileOutcome {
  /** 最后一次查到的提案；一次都没查到为 null */
  proposal: HitlProposal | null
  /** 被组件卸载或切换提案中止：调用方不要再更新界面 */
  aborted: boolean
}

/**
 * 返回一个 reconcile 函数：发起核对，并在 resetKey 变化或组件卸载时自动中止。
 *
 * Args:
 *   resetKey: 变化即中止进行中的核对（如会话 ID、提案 ID）
 */
export function useHitlReconcile(
  resetKey: string | number | null,
): (
  proposalId: number,
  onUpdate?: (proposal: HitlProposal) => void,
) => Promise<ReconcileOutcome> {
  const controllerRef = useRef<AbortController | null>(null)

  useEffect(() => {
    return () => {
      controllerRef.current?.abort()
    }
  }, [resetKey])

  return useCallback(async (proposalId, onUpdate) => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    const proposal = await pollHitlProposalUntilSettled(proposalId, {
      signal: controller.signal,
      onUpdate,
    })
    return { proposal, aborted: controller.signal.aborted }
  }, [])
}
