/** useAuth 单测：登录、退出都会让进行中的刷新作废（R6 的登录世代） */

// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({
  default: { post: vi.fn(), get: vi.fn() },
  setAccessToken: vi.fn(),
  refreshAccessToken: vi.fn(),
  markSessionChanged: vi.fn(),
}))

import api, { markSessionChanged } from "@/lib/api"

import { useAuth } from "./use-auth"

const mockPost = vi.mocked(api.post)
const mockGet = vi.mocked(api.get)

describe("useAuth 登录世代", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal("location", { href: "" })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("登录成功后换代：迟到的旧刷新结果不会覆盖新登录", async () => {
    mockPost.mockResolvedValue({ data: { data: { access_token: "new-token" } } })
    mockGet.mockResolvedValue({ data: { data: { id: 1, permissions: [] } } })
    const { result } = renderHook(() => useAuth())

    await act(async () => {
      await result.current.login({ username: "admin", password: "secret" })
    })

    expect(markSessionChanged).toHaveBeenCalledTimes(1)
  })

  it("退出登录也换代，并通知其它标签页", async () => {
    mockPost.mockResolvedValue({ data: {} })
    const { result } = renderHook(() => useAuth())

    await act(async () => {
      await result.current.logout()
    })

    expect(markSessionChanged).toHaveBeenCalledTimes(1)
  })
})
