/** OpsAssistantPage 单测：完全访问确认绑定打开弹窗时的会话 */

// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { AgentSession } from "@/types/agent"
import type { OpsChatItem } from "@/hooks/use-ops-chat"

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

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

import { useOpsChat } from "@/hooks/use-ops-chat"
import {
  getDeviceQueryResult,
  listAgentSessions,
  patchAgentSession,
} from "@/lib/agent-api"

const mockListAgentSessions = vi.mocked(listAgentSessions)
const mockPatchAgentSession = vi.mocked(patchAgentSession)
const mockUseOpsChat = vi.mocked(useOpsChat)
const mockGetDeviceQueryResult = vi.mocked(getDeviceQueryResult)

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
