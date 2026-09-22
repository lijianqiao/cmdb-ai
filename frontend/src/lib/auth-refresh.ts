/** 跨标签页的 access_token 刷新协调（R6）

 * 为什么需要：refresh_token 是一次性的。同一浏览器的两个标签页共享同一个 refresh cookie，
 * 如果它们几乎同时拿着同一个旧 cookie 去刷新，先到的那个完成轮换，后到的会被后端当成
 * 重放——整族登录会话被撤销，两个标签页一起掉线。这个保护本身有安全价值，不能关掉。
 *
 * 做法：
 * 1. 页面内：启动恢复、401 拦截器等所有调用方共用一个进行中的刷新 Promise。
 * 2. 跨标签页：Web Locks 互斥，同一时刻只有持锁的标签页真正发刷新请求，后一个标签页
 *    拿到锁时，前一个的响应已经把新 cookie 写好了，所以它即使再刷新也是合法轮换，
 *    不会被当成重放——这是正确性的来源。
 * 3. 持锁方把新 token 经 BroadcastChannel 发给其它标签页；排队等锁期间收到它的标签页
 *    直接复用，省掉一次轮换（尽力而为，收不到就自己刷新，照样安全）。新 token 只在
 *    内存里传递，不写 localStorage。
 * 4. 登录世代：本页登录/退出、或收到其它标签页的登录/退出广播都会换代；换代前发起的
 *    刷新结果一律作废，退出之后不会被迟到的刷新把旧会话写回来。
 * 5. 有限等待：刷新请求本身有超时（由调用方的 HTTP 客户端设置），持锁标签页关闭或崩溃时
 *    浏览器自动释放锁，等锁也有上限——任何情况下都不会永久挂住。
 * 6. 浏览器不支持 Web Locks（非 HTTPS 页面或旧浏览器）时只能做到页面内去重，
 *    supportsCrossTab 为 false，由调用方明确提示多标签页可能被要求重新登录。
 */

const REFRESH_LOCK_NAME = "ent-agent:auth-refresh"

/** Web Locks 中本模块用到的部分（navigator.locks 满足这个形状） */
export interface AuthLocks {
  request<T>(
    name: string,
    options: { signal?: AbortSignal },
    callback: () => Promise<T>,
  ): Promise<T>
}

/** BroadcastChannel 中本模块用到的部分 */
export interface AuthChannel {
  postMessage(data: unknown): void
  addEventListener(type: "message", listener: (event: { data: unknown }) => void): void
}

type AuthMessage = { type: "refreshed"; token: string } | { type: "session_changed" }

function isAuthMessage(data: unknown): data is AuthMessage {
  if (typeof data !== "object" || data === null) return false
  const message = data as { type?: unknown; token?: unknown }
  return (
    (message.type === "refreshed" && typeof message.token === "string") ||
    message.type === "session_changed"
  )
}

/** 刷新期间本页或其它标签页登录/退出了，这次刷新的结果不能再用 */
export class RefreshSupersededError extends Error {
  constructor() {
    super("登录状态在刷新期间发生变化，本次刷新结果已作废")
    this.name = "RefreshSupersededError"
  }
}

export interface RefreshCoordinatorOptions {
  /** navigator.locks；不支持时传 null，只做页面内去重 */
  locks: AuthLocks | null
  /** 同源标签页间的广播通道；不支持时传 null */
  channel: AuthChannel | null
  /** 真正发出刷新请求，返回新的 access_token；须自带超时 */
  requestRefresh: () => Promise<string>
  /** 排队等锁的上限；应大于刷新请求超时，避免持锁方还在正常刷新时就放弃 */
  lockWaitTimeoutMs: number
}

export interface RefreshCoordinator {
  /** 拿一个新的 access_token；同一页面内并发调用共用同一次刷新 */
  refresh(): Promise<string>
  /** 本页登录或退出：换代，并通知其它标签页作废各自进行中的刷新 */
  sessionChanged(): void
  /** 是否真的能跨标签页互斥 */
  readonly supportsCrossTab: boolean
}

export function createRefreshCoordinator(
  options: RefreshCoordinatorOptions,
): RefreshCoordinator {
  const { locks, channel, requestRefresh, lockWaitTimeoutMs } = options
  let generation = 0
  let inflight: Promise<string> | null = null
  // 用递增序号而不是时间戳比先后，避免同一毫秒内分不清
  let sequence = 0
  let shared: { token: string; receivedAt: number } | null = null

  channel?.addEventListener("message", (event) => {
    const message = event.data
    if (!isAuthMessage(message)) return
    if (message.type === "refreshed") {
      shared = { token: message.token, receivedAt: ++sequence }
    } else {
      generation += 1
      shared = null
    }
  })

  async function runRefresh(): Promise<string> {
    const startedGeneration = generation
    const startedAt = ++sequence
    const refreshOrReuse = async (): Promise<string> => {
      // 等锁期间别的标签页刚刷新过：直接用它换来的新 token，不再轮换
      if (shared !== null && shared.receivedAt > startedAt) return shared.token
      const token = await requestRefresh()
      channel?.postMessage({ type: "refreshed", token } satisfies AuthMessage)
      return token
    }
    const token =
      locks === null
        ? await refreshOrReuse()
        : await locks.request(
            REFRESH_LOCK_NAME,
            { signal: AbortSignal.timeout(lockWaitTimeoutMs) },
            refreshOrReuse,
          )
    if (generation !== startedGeneration) throw new RefreshSupersededError()
    return token
  }

  return {
    refresh(): Promise<string> {
      if (inflight === null) {
        inflight = runRefresh().finally(() => {
          inflight = null
        })
      }
      return inflight
    },
    sessionChanged(): void {
      generation += 1
      shared = null
      channel?.postMessage({ type: "session_changed" } satisfies AuthMessage)
    },
    supportsCrossTab: locks !== null,
  }
}
