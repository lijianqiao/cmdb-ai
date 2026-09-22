/** HitlApprovalDialog 单测：弹窗主动弹出、动态凭据口令输入与批准/拒绝操作 */

// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { AxiosError, AxiosHeaders } from "axios"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { HitlProposal } from "@/lib/hitl-api"
import { HitlApprovalDialog, type HitlApprovalDialogProps } from "./HitlApprovalDialog"

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
}))

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
  },
}))

import { usePermission } from "@/hooks/use-permission"
import { decideHitlProposal, getHitlProposal } from "@/lib/hitl-api"

const mockUsePermission = vi.mocked(usePermission)
const mockGetHitlProposal = vi.mocked(getHitlProposal)
const mockDecideHitlProposal = vi.mocked(decideHitlProposal)

/** 模拟服务端返回的 HTTP 错误（readErrorMessage 会读出 detail） */
function httpError(status: number, detail: string): AxiosError {
  return new AxiosError(
    `Request failed with status code ${status}`,
    status >= 500 ? AxiosError.ERR_BAD_RESPONSE : AxiosError.ERR_BAD_REQUEST,
    undefined,
    undefined,
    {
      status,
      statusText: "",
      data: { detail },
      headers: {},
      config: { headers: new AxiosHeaders() },
    },
  )
}

/** 模拟请求根本没到服务端（断网、代理断开） */
function networkError(): AxiosError {
  return new AxiosError("Network Error", AxiosError.ERR_NETWORK)
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

describe("HitlApprovalDialog 模态弹窗与动态凭据口令", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockUsePermission.mockReturnValue({
      permissions: ["agent:hitl_approve"],
      hasPermission: () => true,
      hasAnyPermission: () => true,
      hasAllPermissions: () => true,
    })
  })

  it("打开弹窗时渲染载荷与口令输入框", () => {
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

    // 口令框存在，且没填口令时批准按钮受限
    const otpInput = screen.getByTestId("hitl-dynamic-password")
    expect(otpInput).toBeInTheDocument()
    expect(screen.getByTestId("hitl-approve-button")).toBeDisabled()
  })

  it("输入口令后允许批准，并调用 onApprove 提交", async () => {
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

describe("HitlApprovalDialog 动态凭据口令原样提交", () => {
  /** 合成的测试口令：8 位以上、字母数字符号混合、带首尾空白（不是真实密码） */
  const SYNTHETIC_PASSWORD = "  Abc12345!@# "

  beforeEach(() => {
    vi.clearAllMocks()
    mockUsePermission.mockReturnValue({
      permissions: ["agent:hitl_approve"],
      hasPermission: () => true,
      hasAnyPermission: () => true,
      hasAllPermissions: () => true,
    })
  })

  function dialogFor(proposalId: number, onApprove = vi.fn()) {
    return (
      <HitlApprovalDialog
        open={true}
        onOpenChange={vi.fn()}
        sessionId={10}
        proposalId={proposalId}
        actionType="device_query"
        status="PENDING"
        reason="排查交换机"
        assetId={9}
        detail={buildProposal({ id: proposalId })}
        onApprove={onApprove}
      />
    )
  }

  it("onApprove 收到的口令与输入完全一致：不截成 6 位，也不去掉首尾空白", () => {
    const onApprove = vi.fn()
    render(dialogFor(1, onApprove))

    const input = screen.getByTestId("hitl-dynamic-password")
    fireEvent.change(input, { target: { value: SYNTHETIC_PASSWORD } })
    expect(input).toHaveValue(SYNTHETIC_PASSWORD)
    fireEvent.click(screen.getByTestId("hitl-approve-button"))

    expect(onApprove).toHaveBeenCalledWith(SYNTHETIC_PASSWORD)
  })

  it("口令框是掩码密码框，长度上限与后端一致（256）", () => {
    render(dialogFor(1))

    const input = screen.getByTestId("hitl-dynamic-password")
    expect(input).toHaveAttribute("type", "password")
    expect(input).toHaveAttribute("maxlength", "256")
  })

  it("切换到另一个提案时清空已输入的口令", () => {
    const { rerender } = render(dialogFor(1))
    fireEvent.change(screen.getByTestId("hitl-dynamic-password"), {
      target: { value: SYNTHETIC_PASSWORD },
    })

    rerender(dialogFor(2))

    expect(screen.getByTestId("hitl-dynamic-password")).toHaveValue("")
  })
})

describe("HitlApprovalDialog 详情未就绪时禁止批准/重试", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGetHitlProposal.mockReset()
    mockDecideHitlProposal.mockReset()
    mockUsePermission.mockReturnValue({
      permissions: ["agent:hitl_approve"],
      hasPermission: () => true,
      hasAnyPermission: () => true,
      hasAllPermissions: () => true,
    })
  })

  /** 不传 detail：和运维助手页面一样，由弹窗自己拉详情 */
  function selfLoadingDialog(props: Partial<HitlApprovalDialogProps> = {}) {
    return (
      <HitlApprovalDialog
        open={true}
        onOpenChange={vi.fn()}
        sessionId={10}
        proposalId={1}
        actionType="device_query"
        status="PENDING"
        reason="排查交换机"
        assetId={9}
        {...props}
      />
    )
  }

  function staticProposal(id: number, command: string): HitlProposal {
    return buildProposal({
      id,
      asset_credential_type: "static",
      action_payload: { asset_id: 9, command },
    })
  }

  it.each([
    ["403", httpError(403, "无权查看该提案"), "无权查看该提案"],
    ["404", httpError(404, "提案不存在"), "提案不存在"],
    ["503", httpError(503, "服务暂不可用"), "服务暂不可用"],
    ["网络中断", networkError(), "加载审批详情失败"],
  ])("详情加载失败（%s）时批准按钮禁用，点击也不发请求", async (_label, error, message) => {
    mockGetHitlProposal.mockRejectedValue(error)
    const onApprove = vi.fn()
    render(selfLoadingDialog({ onApprove }))

    expect(await screen.findByText(message)).toBeInTheDocument()
    const approveButton = screen.getByTestId("hitl-approve-button")
    expect(approveButton).toBeDisabled()
    fireEvent.click(approveButton)
    expect(onApprove).not.toHaveBeenCalled()
    expect(mockDecideHitlProposal).not.toHaveBeenCalled()
  })

  it("APPROVED 但详情加载失败时重试按钮禁用，点击也不发请求", async () => {
    mockGetHitlProposal.mockRejectedValue(httpError(503, "服务暂不可用"))
    const onRetry = vi.fn()
    render(selfLoadingDialog({ status: "APPROVED", onRetry }))

    expect(await screen.findByText("服务暂不可用")).toBeInTheDocument()
    const retryButton = screen.getByTestId("hitl-retry-button")
    expect(retryButton).toBeDisabled()
    fireEvent.click(retryButton)
    expect(onRetry).not.toHaveBeenCalled()
  })

  it("切换到新提案且新详情加载失败时，不沿用上一个提案的详情", async () => {
    mockGetHitlProposal
      .mockResolvedValueOnce(staticProposal(1, "proposal-1-command"))
      .mockRejectedValueOnce(httpError(503, "服务暂不可用"))
    const onApprove = vi.fn()

    const { rerender } = render(selfLoadingDialog({ proposalId: 1, onApprove }))
    expect(await screen.findByText(/proposal-1-command/)).toBeInTheDocument()

    rerender(selfLoadingDialog({ proposalId: 2, onApprove }))
    expect(await screen.findByText("服务暂不可用")).toBeInTheDocument()

    expect(screen.queryByText(/proposal-1-command/)).not.toBeInTheDocument()
    const approveButton = screen.getByTestId("hitl-approve-button")
    expect(approveButton).toBeDisabled()
    fireEvent.click(approveButton)
    expect(onApprove).not.toHaveBeenCalled()
  })

  it("上一个提案的详情在切换后才返回时被丢弃", async () => {
    let resolveOldDetail!: (value: HitlProposal) => void
    mockGetHitlProposal
      .mockReturnValueOnce(
        new Promise<HitlProposal>((resolve) => {
          resolveOldDetail = resolve
        }),
      )
      .mockRejectedValueOnce(httpError(503, "服务暂不可用"))
    const onApprove = vi.fn()

    const { rerender } = render(selfLoadingDialog({ proposalId: 1, onApprove }))
    rerender(selfLoadingDialog({ proposalId: 2, onApprove }))
    expect(await screen.findByText("服务暂不可用")).toBeInTheDocument()

    await act(async () => {
      resolveOldDetail(staticProposal(1, "proposal-1-command"))
    })

    expect(screen.queryByText(/proposal-1-command/)).not.toBeInTheDocument()
    const approveButton = screen.getByTestId("hitl-approve-button")
    expect(approveButton).toBeDisabled()
    fireEvent.click(approveButton)
    expect(onApprove).not.toHaveBeenCalled()
  })

  it("父组件传入的详情不属于当前提案时不能批准", () => {
    render(
      selfLoadingDialog({
        proposalId: 1,
        detail: staticProposal(2, "proposal-2-command"),
      }),
    )

    expect(screen.getByTestId("hitl-approve-button")).toBeDisabled()
  })

  it("重新加载详情成功后才恢复批准，且只调用一次 onApprove", async () => {
    mockGetHitlProposal.mockRejectedValueOnce(httpError(503, "服务暂不可用"))
    const onApprove = vi.fn()
    render(selfLoadingDialog({ onApprove }))

    const reloadButton = await screen.findByRole("button", { name: "重新加载详情" })
    expect(screen.getByTestId("hitl-approve-button")).toBeDisabled()

    mockGetHitlProposal.mockResolvedValueOnce(staticProposal(1, "show version"))
    fireEvent.click(reloadButton)

    await waitFor(() => {
      expect(screen.getByTestId("hitl-approve-button")).toBeEnabled()
    })
    fireEvent.click(screen.getByTestId("hitl-approve-button"))

    expect(onApprove).toHaveBeenCalledTimes(1)
    expect(onApprove).toHaveBeenCalledWith("")
    expect(mockGetHitlProposal).toHaveBeenCalledTimes(2)
  })
})
