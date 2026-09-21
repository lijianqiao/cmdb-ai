/** use-hitl-reconcile 单测：请求没拿到明确答复后，按提案 ID 有上限地退避查询 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/hitl-api", () => ({
  getHitlProposal: vi.fn(),
  decideHitlProposal: vi.fn(),
  retryHitlProposal: vi.fn(),
}))

import {
  decideHitlProposal,
  getHitlProposal,
  retryHitlProposal,
  type HitlProposal,
} from "@/lib/hitl-api"

import { pollHitlProposalUntilSettled } from "./use-hitl-reconcile"

const mockGetHitlProposal = vi.mocked(getHitlProposal)

function proposal(status: string): HitlProposal {
  return {
    id: 1,
    session_id: 10,
    proposed_by_agent_id: "agent-1",
    action_type: "device_control",
    action_payload: { asset_id: 9 },
    status,
    reviewed_by_user_id: 1,
    reviewed_at: "2026-09-21T10:00:00Z",
    executed_at: null,
    created_at: "2026-09-21T09:59:00Z",
  }
}

describe("pollHitlProposalUntilSettled", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    mockGetHitlProposal.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it("第一次就查到落定状态：立即返回，只查一次", async () => {
    mockGetHitlProposal.mockResolvedValue(proposal("EXECUTED"))

    const result = await pollHitlProposalUntilSettled(1, {
      signal: new AbortController().signal,
    })

    expect(result?.status).toBe("EXECUTED")
    expect(mockGetHitlProposal).toHaveBeenCalledTimes(1)
    expect(mockGetHitlProposal).toHaveBeenCalledWith(1)
  })

  it("还在执行就按退避继续查，直到落定；全程只查状态，不重新批准或重试", async () => {
    mockGetHitlProposal
      .mockResolvedValueOnce(proposal("EXECUTING"))
      .mockResolvedValueOnce(proposal("EXECUTING"))
      .mockResolvedValueOnce(proposal("UNKNOWN"))
    const onUpdate = vi.fn()

    const pending = pollHitlProposalUntilSettled(1, {
      signal: new AbortController().signal,
      onUpdate,
    })
    await vi.advanceTimersByTimeAsync(1_000 + 2_000)
    const result = await pending

    expect(result?.status).toBe("UNKNOWN")
    expect(mockGetHitlProposal).toHaveBeenCalledTimes(3)
    expect(onUpdate.mock.calls.map(([p]) => (p as HitlProposal).status)).toEqual([
      "EXECUTING",
      "EXECUTING",
      "UNKNOWN",
    ])
    expect(decideHitlProposal).not.toHaveBeenCalled()
    expect(retryHitlProposal).not.toHaveBeenCalled()
  })

  it("审批可能还在路上（PENDING）时也继续查", async () => {
    mockGetHitlProposal
      .mockResolvedValueOnce(proposal("PENDING"))
      .mockResolvedValueOnce(proposal("EXECUTED"))

    const pending = pollHitlProposalUntilSettled(1, {
      signal: new AbortController().signal,
    })
    await vi.advanceTimersByTimeAsync(1_000)

    expect((await pending)?.status).toBe("EXECUTED")
  })

  it("查询本身失败（仍在断网）时不放弃，按节奏接着查", async () => {
    mockGetHitlProposal
      .mockRejectedValueOnce(new Error("Network Error"))
      .mockResolvedValueOnce(proposal("EXECUTED"))

    const pending = pollHitlProposalUntilSettled(1, {
      signal: new AbortController().signal,
    })
    await vi.advanceTimersByTimeAsync(1_000)

    expect((await pending)?.status).toBe("EXECUTED")
  })

  it("到总时限仍在执行就停下：返回最后一次查到的状态，查询次数有上限", async () => {
    mockGetHitlProposal.mockResolvedValue(proposal("EXECUTING"))

    const pending = pollHitlProposalUntilSettled(1, {
      signal: new AbortController().signal,
    })
    await vi.advanceTimersByTimeAsync(10 * 60_000)

    expect((await pending)?.status).toBe("EXECUTING")
    expect(mockGetHitlProposal.mock.calls.length).toBeLessThanOrEqual(12)
  })

  it("一次都没查到时返回 null", async () => {
    mockGetHitlProposal.mockRejectedValue(new Error("Network Error"))

    const pending = pollHitlProposalUntilSettled(1, {
      signal: new AbortController().signal,
    })
    await vi.advanceTimersByTimeAsync(10 * 60_000)

    expect(await pending).toBeNull()
  })

  it("中止后（组件卸载/切换提案）不再发起查询，也不再回调", async () => {
    mockGetHitlProposal.mockResolvedValue(proposal("EXECUTING"))
    const controller = new AbortController()
    const onUpdate = vi.fn()

    const pending = pollHitlProposalUntilSettled(1, {
      signal: controller.signal,
      onUpdate,
    })
    await vi.advanceTimersByTimeAsync(0)
    controller.abort()
    await vi.advanceTimersByTimeAsync(60_000)
    await pending

    expect(mockGetHitlProposal).toHaveBeenCalledTimes(1)
    expect(onUpdate).toHaveBeenCalledTimes(1)
  })
})
