/** TurnUsageLine 单测：数字格式与单模型时不挂 tooltip */

// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, describe, expect, it } from "vitest"

import { TurnUsageLine } from "./TurnUsageLine"

afterEach(() => {
  cleanup()
})

describe("TurnUsageLine", () => {
  it("展示输入 / 输出 / 合计与花费", () => {
    render(
      <TurnUsageLine
        usage={{
          promptTokens: 12483,
          completionTokens: 856,
          costUsd: 0.0021,
          byModel: null,
        }}
      />,
    )

    const line = screen.getByText(/输入/)
    expect(line).toHaveTextContent("输入 12,483")
    expect(line).toHaveTextContent("输出 856")
    // 合计是两者之和，不是某一次调用的用量
    expect(line).toHaveTextContent("合计 13,339 tokens")
    expect(line).toHaveTextContent("$0.0021")
  })

  it("金额较大时收敛到两位小数", () => {
    render(
      <TurnUsageLine
        usage={{
          promptTokens: 1,
          completionTokens: 1,
          costUsd: 1.239,
          byModel: null,
        }}
      />,
    )

    expect(screen.getByText(/输入/)).toHaveTextContent("$1.24")
  })

  it("只有一个模型时不挂 tooltip——明细和汇总是同一份数字", () => {
    render(
      <TurnUsageLine
        usage={{
          promptTokens: 100,
          completionTokens: 20,
          costUsd: 0.001,
          byModel: {
            "local-chat": {
              prompt_tokens: 100,
              completion_tokens: 20,
              cost_usd: 0.001,
            },
          },
        }}
      />,
    )

    expect(screen.getByText(/输入/)).not.toHaveAttribute("aria-describedby")
  })
})
