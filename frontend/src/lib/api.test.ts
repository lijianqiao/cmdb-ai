/** HTTP 客户端 401 处理单测（R6）

 * 并发的 401 共用同一次刷新；每个请求最多重放一次，重放后仍 401 就放弃，
 * 不会形成刷新循环。测试环境（jsdom）没有 Web Locks，正好覆盖「只能页面内去重」
 * 时必须明确提示用户的分支。
 */

// @vitest-environment jsdom

import axios, {
  AxiosError,
  AxiosHeaders,
  type AxiosAdapter,
  type InternalAxiosRequestConfig,
} from "axios"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("sonner", () => ({
  toast: { warning: vi.fn(), error: vi.fn(), success: vi.fn() },
}))

import api, { getAccessToken, setAccessToken } from "./api"

function respond(config: InternalAxiosRequestConfig, status: number) {
  const response = {
    data: { code: status, data: status === 200 ? "ok" : null, message: "" },
    status,
    statusText: "",
    headers: {},
    config,
  }
  if (status >= 400) {
    return Promise.reject(
      new AxiosError(`Request failed with status code ${status}`, "ERR", config, null, response),
    )
  }
  return Promise.resolve(response)
}

/** 只认新 token 的后端：带旧 token 或不带 token 一律 401 */
function adapterAccepting(token: string): AxiosAdapter {
  return (config) => {
    const authorization = AxiosHeaders.from(config.headers).get("Authorization")
    return respond(config, authorization === `Bearer ${token}` ? 200 : 401)
  }
}

describe("api 401 处理", () => {
  const originalAdapter = api.defaults.adapter
  let refreshSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    vi.clearAllMocks()
    setAccessToken("expired-token")
    refreshSpy = vi.spyOn(axios, "post").mockResolvedValue({
      data: { data: { access_token: "fresh-token" } },
    })
  })

  afterEach(() => {
    api.defaults.adapter = originalAdapter
    refreshSpy.mockRestore()
  })

  it("并发的 401 只刷新一次，每个请求带新 token 重放一次", async () => {
    api.defaults.adapter = adapterAccepting("fresh-token")

    const results = await Promise.all([api.get("/a"), api.get("/b"), api.get("/c")])

    expect(results.map((result) => result.status)).toEqual([200, 200, 200])
    expect(refreshSpy).toHaveBeenCalledTimes(1)
    expect(getAccessToken()).toBe("fresh-token")
  })

  it("重放后仍然 401 就放弃，排队等刷新的请求也一样，不会再次刷新形成循环", async () => {
    api.defaults.adapter = adapterAccepting("never-valid")

    const results = await Promise.allSettled([api.get("/a"), api.get("/b"), api.get("/c")])

    expect(results.every((result) => result.status === "rejected")).toBe(true)
    expect(refreshSpy).toHaveBeenCalledTimes(1)
  })

  it("刷新请求设置了有限超时，不会无限期等待", async () => {
    api.defaults.adapter = adapterAccepting("fresh-token")

    await api.get("/a")

    const [, , config] = refreshSpy.mock.calls[0] as [string, unknown, { timeout?: number }]
    expect(config.timeout).toBeGreaterThan(0)
  })

  it("浏览器不支持 Web Locks 时明确提示多标签页的限制，只提示一次", async () => {
    // 「是否已提示」是模块级状态，前面的用例已经触发过，这里用一份全新的模块实例
    vi.resetModules()
    const fresh = await import("./api")
    const { toast: freshToast } = await import("sonner")
    fresh.default.defaults.adapter = adapterAccepting("fresh-token")
    fresh.setAccessToken("expired-token")

    await fresh.default.get("/a")
    fresh.setAccessToken("expired-again")
    await fresh.default.get("/b")

    expect(freshToast.warning).toHaveBeenCalledTimes(1)
    expect(vi.mocked(freshToast.warning).mock.calls[0]?.[0]).toMatch(/标签页/)
  })
})
