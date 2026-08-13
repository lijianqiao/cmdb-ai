/** OperationsConfigCard 说明文案渲染单测 */

// @vitest-environment jsdom

import { render, screen } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { describe, expect, it, vi } from "vitest"

import { OperationsConfigCard } from "./OperationsConfigCard"

vi.mock("@/lib/system-config-api", () => ({
  updateOperationsSystemConfig: vi.fn(),
}))

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

describe("OperationsConfigCard", () => {
  it("解释四项运行参数的作用和副作用边界", () => {
    render(
      <OperationsConfigCard
        value={{
          hitl_notify_auto_approve: false,
          monitor_probe_timeout_seconds: 3,
          monitor_sweep_interval_seconds: 30,
          cmdb_diff_interval_seconds: 3600,
          monitor_event_retention_days: 7,
        }}
        onSaved={vi.fn()}
      />,
    )
    expect(
      screen.getByText(/notify 类型提案会跳过人工审批/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/不会自动批准 device_query 或 device_control/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/单个 TCP 连接探测允许等待的最长时间/),
    ).toBeInTheDocument()
    expect(screen.getByText(/全部启用目标探测完成后/)).toBeInTheDocument()
    expect(
      screen.getByText(/只记录差异审计，不自动修改资产/),
    ).toBeInTheDocument()
  })
})
