/** HitlApprovalDialog 单测：弹窗主动弹出、InputOTP 密码输入与批准/拒绝操作 */

// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { HitlProposal } from "@/lib/hitl-api"
import { HitlApprovalDialog } from "./HitlApprovalDialog"

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

import { usePermission } from "@/hooks/use-permission"

const mockUsePermission = vi.mocked(usePermission)

function buildProposal(overrides: Partial<HitlProposal> = {}): HitlProposal {
  return {
    id: 1,
    session_id: 10,
    proposed_by_agent_id: "agent-1",
    action_type: "device_query",
    action_payload: {
      asset_id: 9,
      proposal_reason: "排查交换机",
      command: "show running-config",
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

describe("HitlApprovalDialog 模态弹窗与 InputOTP", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockUsePermission.mockReturnValue({
      permissions: ["agent:hitl_approve"],
      hasPermission: () => true,
      hasAnyPermission: () => true,
      hasAllPermissions: () => true,
    })
  })

  it("打开弹窗时渲染载荷与 InputOTP 输入组件", () => {
    const proposal = buildProposal()

    render(
      <HitlApprovalDialog
        open={true}
        onOpenChange={vi.fn()}
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="PENDING"
        reason="排查交换机"
        assetId={9}
        detail={proposal}
      />,
    )

    // 标题与说明
    expect(screen.getByText("人工审批请求")).toBeInTheDocument()
    expect(screen.getByText("排查交换机")).toBeInTheDocument()
    expect(screen.getByText(/show running-config/)).toBeInTheDocument()

    // InputOTP 存在且批准按钮受限
    const otpInput = screen.getByTestId("hitl-dynamic-password")
    expect(otpInput).toBeInTheDocument()
    expect(screen.getByTestId("hitl-approve-button")).toBeDisabled()
  })

  it("输入 OTP 密码后允许批准，并调用 onApprove 提交", async () => {
    const proposal = buildProposal()
    const onApprove = vi.fn().mockResolvedValue(undefined)
    const onOpenChange = vi.fn()

    render(
      <HitlApprovalDialog
        open={true}
        onOpenChange={onOpenChange}
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="PENDING"
        reason="排查交换机"
        assetId={9}
        detail={proposal}
        onApprove={onApprove}
      />,
    )

    const otpInput = screen.getByTestId("hitl-dynamic-password")
    fireEvent.change(otpInput, { target: { value: "123456" } })

    const approveButton = screen.getByTestId("hitl-approve-button")
    expect(approveButton).not.toBeDisabled()

    fireEvent.click(approveButton)

    await waitFor(() => {
      expect(onApprove).toHaveBeenCalledWith("123456")
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
  })
})
