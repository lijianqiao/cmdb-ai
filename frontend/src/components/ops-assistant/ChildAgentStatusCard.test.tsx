// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, describe, expect, it } from "vitest"

import { ChildAgentStatusCard } from "./ChildAgentStatusCard"

afterEach(() => {
  cleanup()
})

describe("ChildAgentStatusCard", () => {
  it("RUNNING 时展示加载指示", () => {
    render(
      <ChildAgentStatusCard
        childId="c1"
        role="ops_explorer"
        taskBrief="检查资产 42"
        status="RUNNING"
        resultSummary={null}
      />,
    )
    expect(screen.getByTestId("child-agent-c1")).toBeInTheDocument()
    expect(screen.getByText("执行中")).toBeInTheDocument()
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
  })

  it("COMPLETED 时展示结果摘要", () => {
    render(
      <ChildAgentStatusCard
        childId="c2"
        role="ops_explorer"
        taskBrief="检查资产 42"
        status="COMPLETED"
        resultSummary="监控正常"
      />,
    )
    expect(screen.getByText("监控正常")).toBeInTheDocument()
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
  })

  it("FAILED 与 CANCELLED 展示中文终态文案", () => {
    const { rerender } = render(
      <ChildAgentStatusCard
        childId="c3"
        role="ops_explorer"
        taskBrief="检查资产 42"
        status="FAILED"
        resultSummary={null}
      />,
    )
    expect(screen.getByText("执行失败")).toBeInTheDocument()

    rerender(
      <ChildAgentStatusCard
        childId="c3"
        role="ops_explorer"
        taskBrief="检查资产 42"
        status="CANCELLED"
        resultSummary={null}
      />,
    )
    expect(screen.getByText("已取消")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /创建/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /等待/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /关闭/ })).not.toBeInTheDocument()
  })
})
