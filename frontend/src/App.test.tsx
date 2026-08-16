/** App 路由懒加载烟雾测试：动态 import 契约与关键路由语义 */

// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"
import type { ReactElement } from "react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { App } from "@/App"
import appSource from "./App.tsx?raw"
import { ThemeProvider } from "@/components/theme-provider"
import { TooltipProvider } from "@/components/ui/tooltip"
import { PERMISSIONS, ROUTES } from "@/lib/constants"
import { useAuthStore } from "@/store/auth"
import type { CurrentUser } from "@/types/user"

vi.mock("@/hooks/use-auth", () => ({
  useAuth: vi.fn(() => ({
    bootstrap: vi.fn(),
    isInitialized: true,
    token: "test-token",
    user: null,
    permissions: [],
    isAuthenticated: true,
    isLoading: false,
    login: vi.fn(),
    logout: vi.fn(),
    fetchProfile: vi.fn(),
  })),
}))

vi.mock("@/lib/agent-api", () => ({
  listAgentSessions: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 50,
  }),
  createAgentSession: vi.fn(),
  deleteAgentSession: vi.fn(),
  patchAgentSession: vi.fn(),
}))

vi.mock("@/hooks/use-ops-chat", () => ({
  useOpsChat: vi.fn(() => ({
    messages: [],
    isLoadingHistory: false,
    isSending: false,
    inputDisabled: false,
    wsStatus: "open",
    reconnecting: false,
    monitorAlert: null,
    clearMonitorAlert: vi.fn(),
    sendMessage: vi.fn(),
    loadOlder: vi.fn(),
    hasMore: false,
    isLoadingOlder: false,
  })),
}))

vi.mock("@/lib/api", () => ({
  default: {
    get: vi.fn().mockResolvedValue({
      data: {
        data: {
          items: [],
          total: 0,
        },
      },
    }),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  },
  setAccessToken: vi.fn(),
  getAccessToken: vi.fn(),
  refreshAccessToken: vi.fn(),
}))

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

const mockUser: CurrentUser = {
  id: 1,
  username: "admin",
  email: "admin@example.com",
  nickname: "管理员",
  is_active: true,
  is_superuser: false,
  created_at: "2026-08-14T00:00:00Z",
  updated_at: "2026-08-14T00:00:00Z",
  roles: [],
  permissions: [PERMISSIONS.PERMISSION_READ],
}

function seedAuthenticatedUser(permissions: string[] = [PERMISSIONS.PERMISSION_READ]): void {
  useAuthStore.setState({
    token: "test-token",
    user: { ...mockUser, permissions },
    permissions,
    isAuthenticated: true,
    isLoading: false,
    isInitialized: true,
  })
}

function renderApp(ui: ReactElement): ReturnType<typeof render> {
  return render(
    <ThemeProvider>
      <TooltipProvider>{ui}</TooltipProvider>
    </ThemeProvider>,
  )
}

afterEach(() => {
  cleanup()
  useAuthStore.getState().logout()
  useAuthStore.setState({ isInitialized: true })
})

describe("App 路由懒加载", () => {
  beforeEach(() => {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    })
    seedAuthenticatedUser()
  })

  it("loads business pages through dynamic imports", () => {
    expect(appSource).toContain("lazy(() =>")
    expect(appSource).toContain('import("@/pages/OpsAssistantPage")')
    expect(appSource).not.toMatch(
      /^import\s+\{\s*OpsAssistantPage\s*\}\s+from\s+"@\/pages\/OpsAssistantPage"/m,
    )
  })

  it("renders the ops assistant route through suspense", async () => {
    renderApp(
      <MemoryRouter initialEntries={[ROUTES.OPS_ASSISTANT]}>
        <App />
      </MemoryRouter>,
    )
    expect(await screen.findByRole("heading", { name: "运维助手" }, { timeout: 5_000 })).toBeInTheDocument()
  })

  it("renders the permissions route when the user has permission", async () => {
    renderApp(
      <MemoryRouter initialEntries={[ROUTES.PERMISSIONS]}>
        <App />
      </MemoryRouter>,
    )
    expect(await screen.findByRole("heading", { name: "权限管理" })).toBeInTheDocument()
  })

  it("renders the not found route for unknown paths", async () => {
    renderApp(
      <MemoryRouter initialEntries={["/unknown-route"]}>
        <App />
      </MemoryRouter>,
    )
    expect(await screen.findByText("404 页面不存在")).toBeInTheDocument()
  })
})
