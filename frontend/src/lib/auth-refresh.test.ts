/** 跨标签页刷新协调单测（R6）

 * 用假对象模拟同一浏览器里的两个标签页：它们共享 refresh cookie、Web Locks 与
 * BroadcastChannel，各自有独立的内存状态。假后端照真实规则办事——refresh_token 一次性，
 * 带着已经轮换掉的旧 cookie 来刷新就当重放，整族撤销。
 */

import { describe, expect, it } from "vitest"

import {
  type AuthChannel,
  type AuthLocks,
  RefreshSupersededError,
  createRefreshCoordinator,
} from "./auth-refresh"

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** 按名字串行执行回调，等锁期间 signal 中止就放弃排队（与浏览器 Web Locks 一致）。
 * 锁移交给下一个标签页要跨进程，这里让出一个宏任务来模拟：持锁方释放前发出的广播先到。 */
class FakeLocks implements AuthLocks {
  private tail = new Map<string, Promise<unknown>>()

  request<T>(
    name: string,
    options: { signal?: AbortSignal },
    callback: () => Promise<T>,
  ): Promise<T> {
    const previous = this.tail.get(name) ?? Promise.resolve()
    const waitTurn = new Promise<void>((resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(options.signal?.reason), {
        once: true,
      })
      void previous.then(
        () => setTimeout(resolve, 0),
        () => setTimeout(resolve, 0),
      )
    })
    const run = waitTurn.then(() => callback())
    this.tail.set(
      name,
      run.catch(() => undefined),
    )
    return run
  }
}

/** 同一浏览器内的广播总线：发给除自己以外的所有标签页，异步送达 */
class FakeBus {
  private channels: FakeChannel[] = []

  open(): FakeChannel {
    const channel = new FakeChannel(this)
    this.channels.push(channel)
    return channel
  }

  deliver(from: FakeChannel, data: unknown): void {
    for (const channel of this.channels) {
      if (channel !== from) {
        setTimeout(() => channel.receive(data), 0)
      }
    }
  }
}

class FakeChannel implements AuthChannel {
  private listeners: Array<(event: { data: unknown }) => void> = []
  private bus: FakeBus

  constructor(bus: FakeBus) {
    this.bus = bus
  }

  postMessage(data: unknown): void {
    this.bus.deliver(this, data)
  }

  addEventListener(type: "message", listener: (event: { data: unknown }) => void): void {
    if (type === "message") this.listeners.push(listener)
  }

  receive(data: unknown): void {
    for (const listener of this.listeners) listener({ data })
  }
}

/** 一次性 refresh_token 的后端：请求离开浏览器时带上的 cookie 已被轮换就当重放 */
class FakeAuthServer {
  cookie = "refresh-0"
  rotations = 0
  revoked = false
  latencyMs = 20

  async refresh(): Promise<string> {
    const sentCookie = this.cookie
    await delay(this.latencyMs)
    if (this.revoked || sentCookie !== this.cookie) {
      this.revoked = true
      throw new Error("401: refresh token replayed, family revoked")
    }
    this.rotations += 1
    // 响应回到浏览器时 Set-Cookie 生效，所有标签页共享这个新 cookie
    this.cookie = `refresh-${this.rotations}`
    return `access-${this.rotations}`
  }
}

function openTab(
  server: FakeAuthServer,
  bus: FakeBus,
  locks: AuthLocks | null,
): ReturnType<typeof createRefreshCoordinator> {
  return createRefreshCoordinator({
    locks,
    channel: bus.open(),
    requestRefresh: () => server.refresh(),
    lockWaitTimeoutMs: 1000,
  })
}

describe("createRefreshCoordinator", () => {
  it("两个标签页同时刷新：只轮换一次，会话不会被当成重放撤销", async () => {
    const server = new FakeAuthServer()
    const bus = new FakeBus()
    const locks = new FakeLocks()
    const tabA = openTab(server, bus, locks)
    const tabB = openTab(server, bus, locks)

    const [tokenA, tokenB] = await Promise.all([tabA.refresh(), tabB.refresh()])

    expect(server.revoked).toBe(false)
    expect(server.rotations).toBe(1)
    // 等锁的标签页直接复用持锁标签页刚拿到的 token，不再轮换
    expect(tokenB).toBe(tokenA)
  })

  it("没有跨标签页互斥时，同样的并发会把整个登录会话撤销（这正是要修的问题）", async () => {
    const server = new FakeAuthServer()
    const bus = new FakeBus()
    const tabA = openTab(server, bus, null)
    const tabB = openTab(server, bus, null)

    const results = await Promise.allSettled([tabA.refresh(), tabB.refresh()])

    expect(results.map((result) => result.status)).toContain("rejected")
    expect(server.revoked).toBe(true)
    expect(tabA.supportsCrossTab).toBe(false)
  })

  it("同一页面里多个调用方共用一次刷新", async () => {
    const server = new FakeAuthServer()
    const tab = openTab(server, new FakeBus(), new FakeLocks())

    const tokens = await Promise.all([tab.refresh(), tab.refresh(), tab.refresh()])

    expect(new Set(tokens).size).toBe(1)
    expect(server.rotations).toBe(1)
  })

  it("先后两次刷新各自轮换，后一次用的是前一次换来的新 cookie", async () => {
    const server = new FakeAuthServer()
    const locks = new FakeLocks()
    const bus = new FakeBus()
    const tabA = openTab(server, bus, locks)
    const tabB = openTab(server, bus, locks)

    await tabA.refresh()
    await delay(5)
    const later = await tabB.refresh()

    expect(later).toBe("access-2")
    expect(server.revoked).toBe(false)
  })

  it("本页退出登录后，迟到的刷新结果作废，不会把旧会话写回来", async () => {
    const server = new FakeAuthServer()
    server.latencyMs = 50
    const tab = openTab(server, new FakeBus(), new FakeLocks())

    const pending = tab.refresh()
    tab.sessionChanged()

    await expect(pending).rejects.toBeInstanceOf(RefreshSupersededError)
  })

  it("其它标签页登录或退出后，本页进行中的刷新作废", async () => {
    const server = new FakeAuthServer()
    server.latencyMs = 50
    const bus = new FakeBus()
    const locks = new FakeLocks()
    const tabA = openTab(server, bus, locks)
    const tabB = openTab(server, bus, locks)

    const pending = tabB.refresh()
    tabA.sessionChanged()

    await expect(pending).rejects.toBeInstanceOf(RefreshSupersededError)
  })

  it("等锁超时就放弃，不会永远卡住", async () => {
    const locks = new FakeLocks()
    const bus = new FakeBus()
    const stuck = createRefreshCoordinator({
      locks,
      channel: bus.open(),
      requestRefresh: () => new Promise<string>(() => undefined),
      lockWaitTimeoutMs: 1000,
    })
    const waiter = createRefreshCoordinator({
      locks,
      channel: bus.open(),
      requestRefresh: async () => "never",
      lockWaitTimeoutMs: 30,
    })

    void stuck.refresh().catch(() => undefined)
    await delay(5)

    await expect(waiter.refresh()).rejects.toBeTruthy()
  })
})
