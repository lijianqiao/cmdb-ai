/** OpsAssistantPage 单测：完全访问确认绑定打开弹窗时的会话 */

// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { AxiosError, AxiosHeaders } from "axios"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { AgentSession } from "@/types/agent"
import type { OpsChatItem } from "@/hooks/use-ops-chat"
import type { HitlProposal } from "@/lib/hitl-api"

import { OpsAssistantPage } from "./OpsAssistantPage"

vi.mock("@/hooks/use-permission", () => ({
  usePermission: vi.fn(() => ({
    permissions: [],
    hasPermission: () => false,
    hasAnyPermission: () => false,
    hasAllPermissions: () => false,
  })),
}))

vi.mock("@/hooks/use-ops-chat", () => ({
  useOpsChat: vi.fn(() => ({
    messages: [],
    isLoadingHistory: false,
    isSending: false,
    inputDisabled: false,
    wsStatus: "open",
    reconnecting: false,
    monitorAlert: null,
    clearMonitorAlert: vi.fn(),
    sendMessage: vi.fn(),
    reloadSnapshot: vi.fn(),
    loadOlder: vi.fn(),
    hasMore: false,
    isLoadingOlder: false,
  })),
}))

vi.mock("@/components/ops-assistant/ChatInput", () => ({
  ChatInput: ({
    onApprovalModeSelect,
  }: {
    onApprovalModeSelect: (mode: "ask" | "assist" | "full") => void
  }) => (
    <button type="button" onClick={() => onApprovalModeSelect("full")}>
      选择完全访问
    </button>
  ),
}))

vi.mock("@/lib/agent-api", () => ({
  listAgentSessions: vi.fn(),
  createAgentSession: vi.fn(),
  deleteAgentSession: vi.fn(),
  patchAgentSession: vi.fn(),
  getDeviceQueryResult: vi.fn(),
  recoverDeviceQuerySummary: vi.fn(),
}))

vi.mock("@/lib/hitl-api", () => ({
  getHitlProposal: vi.fn(),
  decideHitlProposal: vi.fn(),
  retryHitlProposal: vi.fn(),
  resolveUnknownHitlProposal: vi.fn(),
}))

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
    loading: vi.fn(() => "reconcile-toast"),
    dismiss: vi.fn(),
  },
}))

import { toast } from "sonner"

import { usePermission } from "@/hooks/use-permission"
import { useOpsChat } from "@/hooks/use-ops-chat"
import {
  getDeviceQueryResult,
  listAgentSessions,
  patchAgentSession,
} from "@/lib/agent-api"
import { decideHitlProposal, getHitlProposal } from "@/lib/hitl-api"

const mockListAgentSessions = vi.mocked(listAgentSessions)
const mockPatchAgentSession = vi.mocked(patchAgentSession)
const mockUseOpsChat = vi.mocked(useOpsChat)
const mockGetDeviceQueryResult = vi.mocked(getDeviceQueryResult)
const mockUsePermission = vi.mocked(usePermission)
const mockGetHitlProposal = vi.mocked(getHitlProposal)
const mockDecideHitlProposal = vi.mocked(decideHitlProposal)

afterEach(() => {
  cleanup()
})

function buildSession(id: number, approvalMode: AgentSession["approval_mode"] = "ask"): AgentSession {
  return {
    id,
    user_id: 1,
    title: `会话 #${id}`,
    status: "active",
    approval_mode: approvalMode,
    created_at: "2026-08-14T00:00:00Z",
    updated_at: "2026-08-14T00:00:00Z",
  }
}

async function selectSession(id: number): Promise<void> {
  const titles = await screen.findAllByText(`会话 #${id}`)
  const button = titles[0]?.closest("button")
  if (button == null) {
    throw new Error(`未找到会话 #${id} 的选择按钮`)
  }
  fireEvent.click(button)
}

async function openFullAccessDialog(): Promise<void> {
  fireEvent.click(screen.getByRole("button", { name: "选择完全访问" }))
  await screen.findByText("确认开启完全访问")
}

async function confirmFullAccess(): Promise<void> {
  const dialog = screen.getByRole("dialog")
  fireEvent.click(within(dialog).getByRole("button", { name: "确认" }))
}

describe("OpsAssistantPage 完全访问确认", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    Element.prototype.scrollIntoView = vi.fn()
    mockListAgentSessions.mockResolvedValue({
      items: [buildSession(1), buildSession(2)],
      total: 2,
      page: 1,
      page_size: 50,
    })
    mockPatchAgentSession.mockImplementation(async (sessionId, body) =>
      buildSession(sessionId, body.approval_mode ?? "ask"),
    )
  })

  it("confirms full access for the session that opened the dialog", async () => {
    render(<OpsAssistantPage />)
    await selectSession(1)
    await openFullAccessDialog()
    await selectSession(2)
    expect(screen.queryByRole("button", { name: "确认" })).not.toBeInTheDocument()
    expect(mockPatchAgentSession).not.toHaveBeenCalledWith(2, {
      approval_mode: "full",
    })
  })

  it("patches the session that opened the dialog when confirmed without switching", async () => {
    render(<OpsAssistantPage />)
    await selectSession(1)
    await openFullAccessDialog()
    await confirmFullAccess()

    await waitFor(() => {
      expect(mockPatchAgentSession).toHaveBeenCalledWith(1, {
        approval_mode: "full",
      })
    })
  })
})

describe("OpsAssistantPage 完整配置会话隔离", () => {
  const sharedProposal: OpsChatItem = {
    kind: "hitl",
    id: "hitl:7",
    proposalId: 7,
    actionType: "device_query",
    status: "EXECUTED",
    reason: "排查交换机",
    assetId: 9,
    resultExcerpt: "preview",
    hasFullResult: true,
  }

  beforeEach(() => {
    vi.clearAllMocks()
    mockListAgentSessions.mockResolvedValue({
      items: [buildSession(1), buildSession(2)],
      total: 2,
      page: 1,
      page_size: 50,
    })
    mockUseOpsChat.mockImplementation(({ sessionId }) => ({
      messages: sessionId == null ? [] : [sharedProposal],
      isLoadingHistory: false,
      isSending: false,
      inputDisabled: false,
      wsStatus: "open",
      reconnecting: false,
      monitorAlert: null,
      clearMonitorAlert: vi.fn(),
      sendMessage: vi.fn(),
      cancelTurn: vi.fn(),
      reloadSnapshot: vi.fn(),
      loadOlder: vi.fn(),
      hasMore: false,
      isLoadingOlder: false,
    }))
    mockGetDeviceQueryResult.mockImplementation(async (sessionId) => ({
      proposal_id: 7,
      content: `session ${sessionId}`,
      content_length: 9,
      summary_status: "completed",
      created_at: "2026-08-15T10:00:00Z",
    }))
  })

  it("同一 proposal ID 切换会话后使用当前选中的 session ID", async () => {
    render(<OpsAssistantPage />)
    await selectSession(1)
    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))
    expect(await screen.findByText("session 1")).toBeInTheDocument()

    await selectSession(2)
    expect(screen.queryByText("session 1")).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "查看完整配置" }))

    await waitFor(() => {
      expect(mockGetDeviceQueryResult).toHaveBeenLastCalledWith(2, 7)
    })
    expect(await screen.findByText("session 2")).toBeInTheDocument()
  })
})

describe("OpsAssistantPage 自动弹出的审批窗口", () => {
  const pendingProposal: OpsChatItem = {
    kind: "hitl",
    id: "hitl:901",
    proposalId: 901,
    actionType: "device_control",
    status: "PENDING",
    reason: "重启交换机",
    assetId: 9,
    resultExcerpt: null,
    hasFullResult: false,
  }

  const proposalDetail: HitlProposal = {
    id: 901,
    session_id: 1,
    proposed_by_agent_id: "agent-1",
    action_type: "device_control",
    action_payload: { asset_id: 9, proposal_reason: "重启交换机", command: "reboot" },
    status: "PENDING",
    reviewed_by_user_id: null,
    reviewed_at: null,
    executed_at: null,
    created_at: "2026-09-21T10:00:00Z",
    result_excerpt: null,
    asset_credential_type: "static",
  }

  const serviceUnavailable = new AxiosError(
    "Request failed with status code 503",
    AxiosError.ERR_BAD_RESPONSE,
    undefined,
    undefined,
    {
      status: 503,
      statusText: "",
      data: { detail: "服务暂不可用" },
      headers: {},
      config: { headers: new AxiosHeaders() },
    },
  )

  beforeEach(() => {
    vi.clearAllMocks()
    Element.prototype.scrollIntoView = vi.fn()
    mockGetHitlProposal.mockReset()
    mockDecideHitlProposal.mockReset()
    mockUsePermission.mockReturnValue({
      permissions: ["agent:hitl_approve"],
      hasPermission: () => true,
      hasAnyPermission: () => true,
      hasAllPermissions: () => true,
    })
    mockListAgentSessions.mockResolvedValue({
      items: [buildSession(1)],
      total: 1,
      page: 1,
      page_size: 50,
    })
    mockUseOpsChat.mockImplementation(({ sessionId }) => ({
      messages: sessionId == null ? [] : [pendingProposal],
      isLoadingHistory: false,
      isSending: false,
      inputDisabled: false,
      wsStatus: "open",
      reconnecting: false,
      monitorAlert: null,
      clearMonitorAlert: vi.fn(),
      sendMessage: vi.fn(),
      cancelTurn: vi.fn(),
      reloadSnapshot: vi.fn(),
      loadOlder: vi.fn(),
      hasMore: false,
      isLoadingOlder: false,
    }))
  })

  afterEach(() => {
    // 其它用例按「无审批权限」渲染，别把审批权限漏给它们
    mockUsePermission.mockReturnValue({
      permissions: [],
      hasPermission: () => false,
      hasAnyPermission: () => false,
      hasAllPermissions: () => false,
    })
  })

  it("详情接口 503 时弹窗的批准按钮禁用，不会发出 approve=true", async () => {
    mockGetHitlProposal.mockRejectedValue(serviceUnavailable)
    // 页面会自动选中第一个会话并弹窗；不要再去点侧栏——那算「点弹窗外面」，会把弹窗关掉
    render(<OpsAssistantPage />)

    const dialog = await screen.findByRole("dialog")
    expect(await within(dialog).findByText("服务暂不可用")).toBeInTheDocument()
    const approveButton = within(dialog).getByTestId("hitl-approve-button")
    expect(approveButton).toBeDisabled()
    fireEvent.click(approveButton)
    expect(mockDecideHitlProposal).not.toHaveBeenCalled()
  })

  it("重新加载详情成功后，才发出一次批准请求", async () => {
    mockGetHitlProposal.mockRejectedValue(serviceUnavailable)
    mockDecideHitlProposal.mockResolvedValue({
      ...proposalDetail,
      status: "EXECUTED",
      executed_at: "2026-09-21T10:01:00Z",
    })
    render(<OpsAssistantPage />)

    const dialog = await screen.findByRole("dialog")
    const reloadButton = await within(dialog).findByRole("button", {
      name: "重新加载详情",
    })
    mockGetHitlProposal.mockResolvedValue(proposalDetail)
    fireEvent.click(reloadButton)

    await waitFor(() => {
      expect(within(dialog).getByTestId("hitl-approve-button")).toBeEnabled()
    })
    fireEvent.click(within(dialog).getByTestId("hitl-approve-button"))

    await waitFor(() => {
      expect(mockDecideHitlProposal).toHaveBeenCalledTimes(1)
    })
    expect(mockDecideHitlProposal).toHaveBeenCalledWith(901, { approve: true })
  })

  it("批准请求超时、数据库里实为 UNKNOWN：提示正在核对，最后按真实状态告警，不报成功也不重发", async () => {
    let serverStatus = "PENDING"
    mockGetHitlProposal.mockImplementation(async () => ({
      ...proposalDetail,
      status: serverStatus,
    }))
    mockDecideHitlProposal.mockImplementation(async () => {
      serverStatus = "UNKNOWN"
      throw new AxiosError("timeout of 30000ms exceeded", AxiosError.ECONNABORTED)
    })
    render(<OpsAssistantPage />)

    const dialog = await screen.findByRole("dialog")
    await waitFor(() => {
      expect(within(dialog).getByTestId("hitl-approve-button")).toBeEnabled()
    })
    fireEvent.click(within(dialog).getByTestId("hitl-approve-button"))

    await waitFor(() => {
      expect(toast.warning).toHaveBeenCalledWith(
        expect.stringContaining("执行结果不确定"),
        { id: "reconcile-toast" },
      )
    })
    expect(toast.loading).toHaveBeenCalledWith("请求结果尚未确认，正在核对…")
    expect(toast.success).not.toHaveBeenCalled()
    expect(toast.error).not.toHaveBeenCalled()
    expect(mockDecideHitlProposal).toHaveBeenCalledTimes(1)
  })

  it("动态凭据口令原样到达审批接口：长口令、首尾空白都不改写", async () => {
    // 合成的测试口令，不是真实密码
    const syntheticPassword = " Zx9-long-one-time-pass "
    mockGetHitlProposal.mockResolvedValue({
      ...proposalDetail,
      asset_credential_type: "dynamic",
    })
    mockDecideHitlProposal.mockResolvedValue({
      ...proposalDetail,
      status: "EXECUTED",
      executed_at: "2026-09-21T10:01:00Z",
    })
    render(<OpsAssistantPage />)

    const dialog = await screen.findByRole("dialog")
    const input = await within(dialog).findByTestId("hitl-dynamic-password")
    fireEvent.change(input, { target: { value: syntheticPassword } })
    fireEvent.click(within(dialog).getByTestId("hitl-approve-button"))

    await waitFor(() => {
      expect(mockDecideHitlProposal).toHaveBeenCalledWith(901, {
        approve: true,
        dynamic_credential_password: syntheticPassword,
      })
    })
  })
})
