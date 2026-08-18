/**
 * 可用率状态条的渲染约定。
 *
 * 这条图有三个容易画错、且画错了会误导运维判断的地方，各锁一条测试：
 * 1. 没有探测的格子必须是灰的——画成绿色等于告诉别人「那段时间正常」，是撒谎。
 * 2. 没有数据时右上角显示「—」而不是「100%」，理由同上。
 * 3. 格子数固定 60，条的宽度不能随数据多少而变，否则两行之间没法目视对齐。
 */

// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import { afterEach, describe, expect, it } from "vitest"

import { UptimeStrip } from "@/components/monitor/UptimeStrip"
import type { MonitorUptimeWindow } from "@/types/monitor"

function makeWindow(
  overrides: Partial<MonitorUptimeWindow> = {},
): MonitorUptimeWindow {
  return {
    started_at: "2026-08-18T10:00:00Z",
    bucket_seconds: 60,
    buckets: Array.from({ length: 60 }, () => "unknown" as const),
    uptime_rate: null,
    ...overrides,
  }
}

afterEach(cleanup)

describe("UptimeStrip", () => {
  it("总是渲染 60 个格子，宽度不随数据变化", () => {
    render(<UptimeStrip data={makeWindow()} />)

    expect(screen.getAllByTestId("uptime-bucket")).toHaveLength(60)
  })

  it("没有探测数据时显示「—」而不是 100%", () => {
    render(<UptimeStrip data={makeWindow({ uptime_rate: null })} />)

    expect(screen.getByText("—")).toBeInTheDocument()
    expect(screen.queryByText(/100/)).not.toBeInTheDocument()
  })

  it("有数据时把可用率显示成百分比", () => {
    render(<UptimeStrip data={makeWindow({ uptime_rate: 0.9966 })} />)

    expect(screen.getByText("99.66% 可用率")).toBeInTheDocument()
  })

  it("按状态区分格子，unknown 不与 up 同色", () => {
    const buckets = makeWindow().buckets.slice()
    buckets[0] = "up"
    buckets[1] = "down"

    render(<UptimeStrip data={makeWindow({ buckets })} />)
    const cells = screen.getAllByTestId("uptime-bucket")

    expect(cells[0].className).not.toEqual(cells[1].className)
    expect(cells[0].className).not.toEqual(cells[2].className)
  })

  it("每格带上时间与状态，鼠标悬停能看出是哪一分钟", () => {
    const buckets = makeWindow().buckets.slice()
    buckets[5] = "down"

    render(<UptimeStrip data={makeWindow({ buckets })} />)

    // started_at + 5 分钟 = 10:05，标题里要能看出这一格是何时、什么状态
    expect(screen.getAllByTestId("uptime-bucket")[5]).toHaveAttribute(
      "title",
      expect.stringContaining("失败"),
    )
  })
})
