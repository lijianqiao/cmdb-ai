/** HitlApprovalCard 单测：纯函数校验 + RTL 组件挂载 */

// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { HitlProposal } from "@/lib/hitl-api"
import type { DeviceQueryResult } from "@/types/agent"

import { HitlApprovalCard } from "./HitlApprovalCard"
import {
  isApproveButtonDisabled,
  isRetryAvailable,
  needsDynamicCredentialPassword,
  readLastError,
  shouldShowResultExcerpt,
} from "./hitlApprovalCardUtils"

afterEach(() => {
  cleanup()
})

beforeEach(() => {
  if (typeof window !== "undefined") {
    window.ResizeObserver = class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  }
  if (typeof document !== "undefined") {
    document.elementFromPoint = () => null
  }
})

vi.mock("@/hooks/use-permission", () => ({
  usePermission: vi.fn(),
}))

vi.mock("@/lib/hitl-api", () => ({
  getHitlProposal: vi.fn(),
  decideHitlProposal: vi.fn(),
  retryHitlProposal: vi.fn(),
  resolveUnknownHitlProposal: vi.fn(),
}))

vi.mock("@/lib/agent-api", () => ({
  getDeviceQueryResult: vi.fn(),
  recoverDeviceQuerySummary: vi.fn(),
}))

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

import { usePermission } from "@/hooks/use-permission"
import {
  decideHitlProposal,
  getHitlProposal,
  resolveUnknownHitlProposal,
  retryHitlProposal,
} from "@/lib/hitl-api"
import {
  getDeviceQueryResult,
  recoverDeviceQuerySummary,
} from "@/lib/agent-api"

const mockUsePermission = vi.mocked(usePermission)
const mockGetHitlProposal = vi.mocked(getHitlProposal)
const mockResolveUnknownHitlProposal = vi.mocked(resolveUnknownHitlProposal)
const mockDecideHitlProposal = vi.mocked(decideHitlProposal)
const mockRetryHitlProposal = vi.mocked(retryHitlProposal)
const mockGetDeviceQueryResult = vi.mocked(getDeviceQueryResult)
const mockRecoverDeviceQuerySummary = vi.mocked(recoverDeviceQuerySummary)

function permissionResult(canApprove: boolean) {
  return {
    permissions: canApprove ? ["agent:hitl_approve"] : [],
    hasPermission: (code: string) =>
      canApprove && code === "agent:hitl_approve",
    hasAnyPermission: () => canApprove,
    hasAllPermissions: () => canApprove,
  }
}

function buildProposal(overrides: Partial<HitlProposal> = {}): HitlProposal {
  return {
    id: 1,
    session_id: 10,
    proposed_by_agent_id: "agent-1",
    action_type: "device_query",
    action_payload: {
      asset_id: 9,
      proposal_reason: "排查交换机",
    },
    status: "PENDING",
    reviewed_by_user_id: null,
    reviewed_at: null,
    executed_at: null,
    created_at: "2026-08-12T10:00:00Z",
    result_excerpt: null,
    asset_credential_type: "dynamic",
    ...overrides,
  }
}

function buildDeviceQueryResult(
  overrides: Partial<DeviceQueryResult> = {},
): DeviceQueryResult {
  return {
    proposal_id: 1,
    content: "interface GigabitEthernet1/0/1\n description uplink",
    content_length: 53,
    summary_status: "completed",
    created_at: "2026-08-15T10:00:00Z",
    ...overrides,
  }
}

describe("HitlApprovalCard 执行结果展示（纯函数）", () => {
  it("EXECUTED 且 result_excerpt 有值时应展示", () => {
    expect(shouldShowResultExcerpt("EXECUTED", "show version")).toBe(true)
    expect(shouldShowResultExcerpt("executed", "  output  ")).toBe(true)
  })

  it("非 EXECUTED 或 result_excerpt 为空时不展示", () => {
    expect(shouldShowResultExcerpt("PENDING", "output")).toBe(false)
    expect(shouldShowResultExcerpt("EXECUTED", null)).toBe(false)
    expect(shouldShowResultExcerpt("EXECUTED", "   ")).toBe(false)
  })
})

describe("HitlApprovalCard 动态凭据密码（纯函数）", () => {
  it("device_query + dynamic 凭据时需要密码输入", () => {
    expect(needsDynamicCredentialPassword("device_query", "dynamic")).toBe(true)
  })

  it("其它动作类型或凭据类型不需要密码输入", () => {
    expect(needsDynamicCredentialPassword("device_query", "static")).toBe(false)
    expect(needsDynamicCredentialPassword("notify", "dynamic")).toBe(false)
    expect(needsDynamicCredentialPassword("device_query", null)).toBe(false)
  })

  it("device_control + dynamic 凭据时也需要密码输入", () => {
    expect(needsDynamicCredentialPassword("device_control", "dynamic")).toBe(true)
  })

  it("device_query + dynamic 时密码为空应禁用批准按钮", () => {
    expect(isApproveButtonDisabled(false, false, true, "")).toBe(true)
    expect(isApproveButtonDisabled(false, false, true, "   ")).toBe(true)
  })

  it("填写密码后应允许批准（未在加载/提交中）", () => {
    expect(isApproveButtonDisabled(false, false, true, "secret")).toBe(false)
  })

  it("不需要动态密码时不因密码为空而禁用", () => {
    expect(isApproveButtonDisabled(false, false, false, "")).toBe(false)
  })

  it("加载或提交中始终禁用批准", () => {
    expect(isApproveButtonDisabled(true, false, false, "x")).toBe(true)
    expect(isApproveButtonDisabled(false, true, false, "x")).toBe(true)
  })
})

describe("HitlApprovalCard 重试与失败文案（纯函数）", () => {
  it("有审批权限且状态为 APPROVED 时可重试", () => {
    expect(isRetryAvailable(true, "APPROVED")).toBe(true)
    expect(isRetryAvailable(true, "approved")).toBe(true)
  })

  it("无权限或非 APPROVED 时不展示重试", () => {
    expect(isRetryAvailable(false, "APPROVED")).toBe(false)
    expect(isRetryAvailable(true, "PENDING")).toBe(false)
    expect(isRetryAvailable(true, "EXECUTED")).toBe(false)
  })

  it("从 action_payload 读取 last_error", () => {
    expect(readLastError({ last_error: "连接或执行命令失败" })).toBe(
      "连接或执行命令失败",
    )
    expect(readLastError({ last_error: "  " })).toBeNull()
    expect(readLastError({})).toBeNull()
    expect(readLastError(null)).toBeNull()
  })
})

describe("HitlApprovalCard 组件渲染", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockUsePermission.mockReturnValue({
      permissions: [],
      hasPermission: () => false,
      hasAnyPermission: () => false,
      hasAllPermissions: () => false,
    })
  })

  it("EXECUTED 且 result_excerpt 有值时在 DOM 中展示执行结果", () => {
    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="EXECUTED"
        reason="排查交换机"
        assetId={9}
        resultExcerpt="Cisco IOS XE Software, Version 17.9.4a"
        hasFullResult
      />,
    )

    const excerpt = screen.getByTestId("hitl-result-excerpt")
    expect(excerpt).toBeInTheDocument()
    expect(excerpt).toHaveTextContent("Cisco IOS XE Software, Version 17.9.4a")
  })

  it("device_query + 动态凭据时密码为空则批准按钮禁用", async () => {
    mockUsePermission.mockReturnValue({
      permissions: ["agent:hitl_approve"],
      hasPermission: () => true,
      hasAnyPermission: () => true,
      hasAllPermissions: () => true,
    })
    mockGetHitlProposal.mockResolvedValue(buildProposal())

    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="PENDING"
        reason="排查交换机"
        assetId={9}
        hasFullResult={false}
      />,
    )

    await waitFor(() => {
      expect(screen.getByTestId("hitl-dynamic-password")).toBeInTheDocument()
    })

    const approveButton = screen.getByTestId("hitl-approve-button")
    expect(approveButton).toBeDisabled()
  })

  it("APPROVED 且有审批权限时展示上次失败原因与重试按钮", async () => {
    mockUsePermission.mockReturnValue({
      permissions: ["agent:hitl_approve"],
      hasPermission: () => true,
      hasAnyPermission: () => true,
      hasAllPermissions: () => true,
    })
    mockGetHitlProposal.mockResolvedValue(
      buildProposal({
        status: "APPROVED",
        asset_credential_type: "static",
        action_payload: {
          asset_id: 9,
          proposal_reason: "排查交换机",
          last_error: "连接或执行命令失败",
        },
      }),
    )

    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="APPROVED"
        reason="排查交换机"
        assetId={9}
        hasFullResult={false}
      />,
    )

    await waitFor(() => {
      expect(screen.getByTestId("hitl-last-error")).toHaveTextContent(
        "上次执行失败：连接或执行命令失败",
      )
    })
    expect(screen.getByTestId("hitl-retry-button")).toBeInTheDocument()
  })
})

describe("HitlApprovalCard 审批状态会话隔离", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGetHitlProposal.mockReset()
    mockDecideHitlProposal.mockReset()
    mockUsePermission.mockReturnValue(permissionResult(true))
  })

  afterEach(() => {
    mockGetHitlProposal.mockReset()
    mockDecideHitlProposal.mockReset()
  })

  it("切换 sessionId 后忽略旧会话晚到的审批详情载荷", async () => {
    let resolveOldDetail!: (value: HitlProposal) => void
    const oldDetail = new Promise<HitlProposal>((resolve) => {
      resolveOldDetail = resolve
    })
    const newDetail = new Promise<HitlProposal>(() => undefined)
    mockGetHitlProposal
      .mockReturnValueOnce(oldDetail)
      .mockReturnValueOnce(newDetail)

    const { rerender } = render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="PENDING"
        reason="会话 A"
        assetId={9}
        hasFullResult={false}
      />,
    )
    rerender(
      <HitlApprovalCard
        sessionId={20}
        proposalId={1}
        actionType="device_query"
        status="PENDING"
        reason="会话 B"
        assetId={9}
        hasFullResult={false}
      />,
    )

    await act(async () => {
      resolveOldDetail(
        buildProposal({
          action_payload: {
            asset_id: 9,
            proposal_reason: "会话 A",
            command: "session-a-sensitive-command",
          },
        }),
      )
    })

    expect(screen.queryByText(/session-a-sensitive-command/)).not.toBeInTheDocument()
    expect(screen.getByText("会话 B")).toBeInTheDocument()
    expect(mockGetHitlProposal).toHaveBeenCalledTimes(2)
  })

  it("切换 sessionId 时清空旧详情、localStatus 和动态密码", async () => {
    const oldProposal = buildProposal({
      action_payload: {
        asset_id: 9,
        proposal_reason: "会话 A",
        command: "session-a-sensitive-command",
      },
    })
    const newProposal = buildProposal({
      session_id: 20,
      action_payload: {
        asset_id: 9,
        proposal_reason: "会话 B",
        command: "session-b-sensitive-command",
      },
    })
    mockGetHitlProposal
      .mockResolvedValueOnce(oldProposal)
      .mockResolvedValueOnce(newProposal)
    mockDecideHitlProposal.mockResolvedValue(
      buildProposal({
        status: "APPROVED",
        action_payload: oldProposal.action_payload,
      }),
    )

    const { rerender } = render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="PENDING"
        reason="会话 A"
        assetId={9}
        hasFullResult={false}
      />,
    )
    fireEvent.change(await screen.findByTestId("hitl-dynamic-password"), {
      target: { value: "approve-password" },
    })
    fireEvent.click(screen.getByTestId("hitl-approve-button"))
    expect(await screen.findByText("已批准但未执行")).toBeInTheDocument()
    fireEvent.change(screen.getByTestId("hitl-retry-password"), {
      target: { value: "session-a-password" },
    })

    rerender(
      <HitlApprovalCard
        sessionId={20}
        proposalId={1}
        actionType="device_query"
        status="PENDING"
        reason="会话 B"
        assetId={9}
        hasFullResult={false}
      />,
    )

    const newPasswordInput = await screen.findByTestId("hitl-dynamic-password")
    expect(screen.getByText("等待审批")).toBeInTheDocument()
    expect(screen.queryByText(/session-a-sensitive-command/)).not.toBeInTheDocument()
    expect(screen.getByText(/session-b-sensitive-command/)).toBeInTheDocument()
    expect(newPasswordInput).toHaveValue("")
    expect(screen.queryByTestId("hitl-retry-password")).not.toBeInTheDocument()
    expect(mockGetHitlProposal).toHaveBeenCalledTimes(2)
  })

  it("切换 sessionId 时清空旧审批详情错误并重新加载", async () => {
    mockGetHitlProposal
      .mockRejectedValueOnce(new Error("session A failed"))
      .mockReturnValueOnce(new Promise<HitlProposal>(() => undefined))
    const { rerender } = render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="PENDING"
        reason="会话 A"
        assetId={9}
        hasFullResult={false}
      />,
    )
    expect(await screen.findByText("加载审批详情失败")).toBeInTheDocument()

    rerender(
      <HitlApprovalCard
        sessionId={20}
        proposalId={1}
        actionType="device_query"
        status="PENDING"
        reason="会话 B"
        assetId={9}
        hasFullResult={false}
      />,
    )

    await waitFor(() => {
      expect(screen.queryByText("加载审批详情失败")).not.toBeInTheDocument()
      expect(screen.getByText("加载完整载荷…")).toBeInTheDocument()
    })
    expect(mockGetHitlProposal).toHaveBeenCalledTimes(2)
  })
})

describe("HitlApprovalCard 完整设备配置", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockUsePermission.mockReturnValue(permissionResult(false))
  })

  it("点击后才加载完整配置，收起再展开复用局部缓存", async () => {
    mockGetDeviceQueryResult.mockResolvedValue(buildDeviceQueryResult())
    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="EXECUTED"
        reason="排查交换机"
        assetId={9}
        resultExcerpt="interface GigabitEthernet1/0/1"
        hasFullResult
      />,
    )

    expect(mockGetDeviceQueryResult).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))
    expect(screen.getByText("加载完整配置…")).toBeInTheDocument()
    await waitFor(() => {
      expect(mockGetDeviceQueryResult).toHaveBeenCalledWith(10, 1)
    })
    expect((await screen.findByTestId("hitl-full-result")).textContent).toBe(
      "interface GigabitEthernet1/0/1\n description uplink",
    )
    expect(screen.getByText("53 个字符")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "收起完整配置" }))
    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))
    expect(await screen.findByTestId("hitl-full-result")).toBeInTheDocument()
    expect(mockGetDeviceQueryResult).toHaveBeenCalledTimes(1)
  })

  it("加载失败仅在结果区显示错误，并允许重试", async () => {
    mockGetDeviceQueryResult
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(buildDeviceQueryResult())
    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="EXECUTED"
        reason="排查交换机"
        assetId={9}
        resultExcerpt="preview"
        hasFullResult
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))
    expect(await screen.findByText("加载完整配置失败")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "重试加载" }))
    expect(await screen.findByTestId("hitl-full-result")).toBeInTheDocument()
    expect(mockGetDeviceQueryResult).toHaveBeenCalledTimes(2)
    expect(screen.getByText("已执行")).toBeInTheDocument()
  })

  it("旧记录只显示无法恢复提示且不请求接口", () => {
    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="EXECUTED"
        reason="旧查询"
        assetId={9}
        resultExcerpt="preview"
        hasFullResult={false}
      />,
    )

    expect(
      screen.getByText("该历史记录仅保存了预览，无法恢复完整配置。"),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "查看完整配置" }),
    ).not.toBeInTheDocument()
    expect(mockGetDeviceQueryResult).not.toHaveBeenCalled()
  })

  it("pending 总结可恢复，成功后只更新卡片局部状态", async () => {
    mockGetDeviceQueryResult.mockResolvedValue(
      buildDeviceQueryResult({ summary_status: "pending" }),
    )
    mockRecoverDeviceQuerySummary.mockResolvedValue(
      buildDeviceQueryResult({ summary_status: "generating" }),
    )
    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="EXECUTED"
        reason="排查交换机"
        assetId={9}
        resultExcerpt="preview"
        hasFullResult
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))
    fireEvent.click(await screen.findByRole("button", { name: "恢复 AI 总结" }))
    await waitFor(() => {
      expect(mockRecoverDeviceQuerySummary).toHaveBeenCalledWith(10, 1)
    })
    expect(
      screen.queryByRole("button", { name: "恢复 AI 总结" }),
    ).not.toBeInTheDocument()
    expect(screen.getByText("AI 总结生成中")).toBeInTheDocument()
    expect(mockDecideHitlProposal).not.toHaveBeenCalled()
    expect(mockRetryHitlProposal).not.toHaveBeenCalled()
    expect(mockGetDeviceQueryResult).toHaveBeenCalledTimes(1)
  })

  it("generating 总结只显示生成中，不显示恢复按钮", async () => {
    mockGetDeviceQueryResult.mockResolvedValue(
      buildDeviceQueryResult({ summary_status: "generating" }),
    )
    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="EXECUTED"
        reason="排查交换机"
        assetId={9}
        resultExcerpt="preview"
        hasFullResult
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))
    expect(await screen.findByText("AI 总结生成中")).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "恢复 AI 总结" }),
    ).not.toBeInTheDocument()
  })

  it("sessionId 改变时清空旧会话正文并按新会话重新加载", async () => {
    mockGetDeviceQueryResult
      .mockResolvedValueOnce(buildDeviceQueryResult({ content: "session 10" }))
      .mockResolvedValueOnce(buildDeviceQueryResult({ content: "session 20" }))
    const { rerender } = render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="EXECUTED"
        reason="排查交换机"
        assetId={9}
        resultExcerpt="preview"
        hasFullResult
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))
    expect(await screen.findByText("session 10")).toBeInTheDocument()

    rerender(
      <HitlApprovalCard
        sessionId={20}
        proposalId={1}
        actionType="device_query"
        status="EXECUTED"
        reason="排查交换机"
        assetId={9}
        resultExcerpt="preview"
        hasFullResult
      />,
    )
    expect(screen.queryByText("session 10")).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))
    expect(await screen.findByText("session 20")).toBeInTheDocument()
    expect(mockGetDeviceQueryResult).toHaveBeenLastCalledWith(20, 1)
  })

  it("忽略切换会话后才返回的旧总结恢复响应", async () => {
    let resolveRecovery!: (value: DeviceQueryResult) => void
    const recovery = new Promise<DeviceQueryResult>((resolve) => {
      resolveRecovery = resolve
    })
    mockGetDeviceQueryResult.mockResolvedValue(
      buildDeviceQueryResult({ summary_status: "pending" }),
    )
    mockRecoverDeviceQuerySummary.mockReturnValue(recovery)
    const { rerender } = render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="EXECUTED"
        reason="排查交换机"
        assetId={9}
        resultExcerpt="preview"
        hasFullResult
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))
    fireEvent.click(await screen.findByRole("button", { name: "恢复 AI 总结" }))

    rerender(
      <HitlApprovalCard
        sessionId={20}
        proposalId={1}
        actionType="device_query"
        status="EXECUTED"
        reason="排查交换机"
        assetId={9}
        resultExcerpt="preview"
        hasFullResult
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))
    expect(
      await screen.findByRole("button", { name: "恢复 AI 总结" }),
    ).toBeEnabled()

    resolveRecovery(buildDeviceQueryResult({ summary_status: "generating" }))
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "恢复 AI 总结" })).toBeEnabled()
    })
    expect(screen.queryByText("AI 总结生成中")).not.toBeInTheDocument()
  })
})

describe("HitlApprovalCard UNKNOWN 人工处置", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockUsePermission.mockReturnValue(permissionResult(false))
  })

  it("shows two administrator resolutions for UNKNOWN", async () => {
    mockUsePermission.mockReturnValue(permissionResult(true))
    mockGetHitlProposal.mockResolvedValue(buildProposal({ status: "UNKNOWN" }))
    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_control"
        status="UNKNOWN"
        reason="重启接口"
        assetId={9}
        hasFullResult={false}
      />,
    )

    expect(await screen.findByTestId("hitl-confirm-executed-button")).toBeEnabled()
    expect(screen.getByTestId("hitl-allow-retry-button")).toBeEnabled()
    expect(screen.queryByTestId("hitl-retry-button")).not.toBeInTheDocument()
  })

  it("无审批权限时 UNKNOWN 仅展示状态，不显示处置按钮", async () => {
    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_control"
        status="UNKNOWN"
        reason="重启接口"
        assetId={9}
        hasFullResult={false}
      />,
    )

    expect(screen.queryByTestId("hitl-confirm-executed-button")).not.toBeInTheDocument()
    expect(screen.queryByTestId("hitl-allow-retry-button")).not.toBeInTheDocument()
    expect(screen.queryByTestId("hitl-retry-button")).not.toBeInTheDocument()
    expect(screen.getByText("执行结果不确定")).toBeInTheDocument()
  })

  it("点击确认已执行时发送 confirm_executed", async () => {
    mockUsePermission.mockReturnValue(permissionResult(true))
    mockGetHitlProposal.mockResolvedValue(buildProposal({ status: "UNKNOWN" }))
    mockResolveUnknownHitlProposal.mockResolvedValue(
      buildProposal({ status: "EXECUTED", executed_at: "2026-08-14T01:00:00Z" }),
    )

    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_control"
        status="UNKNOWN"
        reason="重启接口"
        assetId={9}
        hasFullResult={false}
      />,
    )

    fireEvent.click(await screen.findByTestId("hitl-confirm-executed-button"))

    await waitFor(() => {
      expect(mockResolveUnknownHitlProposal).toHaveBeenCalledWith(
        1,
        "confirm_executed",
      )
    })
  })

  it("点击允许重试时发送 allow_retry", async () => {
    mockUsePermission.mockReturnValue(permissionResult(true))
    mockGetHitlProposal.mockResolvedValue(buildProposal({ status: "UNKNOWN" }))
    mockResolveUnknownHitlProposal.mockResolvedValue(
      buildProposal({ status: "APPROVED" }),
    )

    render(
      <HitlApprovalCard
        sessionId={10}
        proposalId={1}
        actionType="device_control"
        status="UNKNOWN"
        reason="重启接口"
        assetId={9}
        hasFullResult={false}
      />,
    )

    fireEvent.click(await screen.findByTestId("hitl-allow-retry-button"))

    await waitFor(() => {
      expect(mockResolveUnknownHitlProposal).toHaveBeenCalledWith(1, "allow_retry")
    })
  })
})
