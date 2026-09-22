/** Axios HTTP 客户端实例 + 拦截器

 * - 请求拦截器：自动携带 access_token
 * - 响应拦截器：401 自动刷新 token，失败跳转登录
 * - 刷新经 auth-refresh 协调：页面内与跨标签页同一时刻都只有一次刷新（R6），
 *   避免两个标签页拿同一个一次性 refresh cookie 并发刷新、被后端当成重放撤销整个会话
 */

import axios, {
  type AxiosInstance,
  type InternalAxiosRequestConfig,
} from "axios"
import { toast } from "sonner"

import {
  type AuthLocks,
  RefreshSupersededError,
  createRefreshCoordinator,
} from "@/lib/auth-refresh"
import { ROUTES } from "@/lib/constants"
import { useAuthStore } from "@/store/auth"

const BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api/v1"

// 刷新请求超时：超时后放弃并跳转登录，不无限期挂着
const REFRESH_TIMEOUT_MS = 20_000
// 排队等锁的上限要比刷新超时长，持锁的标签页还在正常刷新时不能先放弃
const REFRESH_LOCK_WAIT_MS = REFRESH_TIMEOUT_MS + 10_000

/** Axios 实例 */
const api: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 30000,
  withCredentials: true,
})

// ===== Token 管理（内存，不持久化） =====
let accessToken: string | null = null

/** 设置 access_token，同时同步到 zustand store，避免两处状态不一致 */
export function setAccessToken(token: string | null): void {
  accessToken = token
  useAuthStore.getState().setToken(token)
}

/** 获取 access_token */
export function getAccessToken(): string | null {
  return accessToken
}

/** 真正发出一次刷新请求；只由协调器调用，其它地方一律走 refreshAccessToken */
async function requestRefresh(): Promise<string> {
  const response = await axios.post(
    `${BASE_URL}/auth/refresh`,
    {},
    { withCredentials: true, timeout: REFRESH_TIMEOUT_MS }
  )
  const newToken = response.data?.data?.access_token
  if (!newToken) {
    throw new Error("No access_token in refresh response")
  }
  return newToken
}

function browserLocks(): AuthLocks | null {
  // Web Locks 只在安全上下文（HTTPS 或 localhost）里存在
  if (typeof navigator === "undefined" || !("locks" in navigator)) return null
  return {
    request: (name, options, callback) => navigator.locks.request(name, options, callback),
  }
}

const refreshCoordinator = createRefreshCoordinator({
  locks: browserLocks(),
  channel:
    typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("ent-agent:auth"),
  requestRefresh,
  lockWaitTimeoutMs: REFRESH_LOCK_WAIT_MS,
})

let warnedNoCrossTabRefresh = false

/** 只能页面内去重时如实告诉用户，而不是假装多标签页已经没问题 */
function warnIfNoCrossTabRefresh(): void {
  if (refreshCoordinator.supportsCrossTab || warnedNoCrossTabRefresh) return
  warnedNoCrossTabRefresh = true
  toast.warning(
    "当前页面不是 HTTPS 或浏览器版本较旧，无法在多个标签页之间同步登录状态；" +
      "同时打开多个标签页时可能被要求重新登录"
  )
}

/** 用 refresh_token cookie 换取新的 access_token（页面内与跨标签页都只刷新一次） */
export async function refreshAccessToken(): Promise<string> {
  warnIfNoCrossTabRefresh()
  const newToken = await refreshCoordinator.refresh()
  setAccessToken(newToken)
  return newToken
}

/** 本页登录或退出后调用：作废进行中的刷新，并通知其它标签页作废它们的 */
export function markSessionChanged(): void {
  refreshCoordinator.sessionChanged()
}

// ===== 请求拦截器：自动携带 access_token =====
api.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    if (accessToken) {
      config.headers.Authorization = `Bearer ${accessToken}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

// ===== 响应拦截器：401 自动刷新 token =====
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config

    // 非 401 错误，或已经重放过一次：直接拒绝，持续 401 不能变成刷新循环
    if (error.response?.status !== 401 || !originalRequest || originalRequest._retry) {
      return Promise.reject(error)
    }

    // 登录接口本身返回 401（账号或密码错误）不触发刷新，交给调用方处理
    if (originalRequest.url?.includes("/auth/login")) {
      return Promise.reject(error)
    }

    // 如果是刷新 token 的请求失败，直接跳转登录
    if (originalRequest.url?.includes("/auth/refresh")) {
      setAccessToken(null)
      window.location.href = ROUTES.LOGIN
      return Promise.reject(error)
    }

    // 每个请求最多重放一次；同时 401 的请求共用同一次刷新
    originalRequest._retry = true
    try {
      const newToken = await refreshAccessToken()
      originalRequest.headers.Authorization = `Bearer ${newToken}`
      return api(originalRequest)
    } catch (refreshError) {
      // 刷新期间登录状态变了（本页或其它标签页登录/退出）：状态归那次登录/退出管，这里不动
      if (refreshError instanceof RefreshSupersededError) {
        return Promise.reject(error)
      }
      setAccessToken(null)
      window.location.href = ROUTES.LOGIN
      return Promise.reject(refreshError)
    }
  }
)

export default api
