/** HitlApprovalCard 单测：纯函数校验 + RTL 组件挂载 */

// @vitest-environment jsdom

import { render, screen, waitFor } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { beforeEach, describe, expect, it, vi } from "vitest"

import type { HitlProposal } from "@/lib/hitl-api"

import { HitlApprovalCard } from "./HitlApprovalCard"
import {
  isApproveButtonDisabled,
  needsDynamicCredentialPassword,
  shouldShowResultExcerpt,
} from "./hitlApprovalCardUtils"

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
  },
}))

import { usePermission } from "@/hooks/use-permission"
import { getHitlProposal } from "@/lib/hitl-api"

const mockUsePermission = vi.mocked(usePermission)
const mockGetHitlProposal = vi.mocked(getHitlProposal)

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
        proposalId={1}
        actionType="device_query"
        status="EXECUTED"
        reason="排查交换机"
        assetId={9}
        resultExcerpt="Cisco IOS XE Software, Version 17.9.4a"
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
        proposalId={1}
        actionType="device_query"
        status="PENDING"
        reason="排查交换机"
        assetId={9}
      />,
    )

    await waitFor(() => {
      expect(screen.getByTestId("hitl-dynamic-password")).toBeInTheDocument()
    })

    const approveButton = screen.getByTestId("hitl-approve-button")
    expect(approveButton).toBeDisabled()
  })
})
