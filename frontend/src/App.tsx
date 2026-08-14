import { lazy, Suspense, useEffect } from "react"
import { Routes, Route } from "react-router"

import { AppLayout } from "@/components/layout/AppLayout"
import { ProtectedRoute } from "@/components/auth/ProtectedRoute"
import { ErrorBoundary } from "@/components/common/ErrorBoundary"
import { Spinner } from "@/components/ui/spinner"
import { useAuth } from "@/hooks/use-auth"
import { PERMISSIONS, ROUTES } from "@/lib/constants"

const AuditLogsPage = lazy(() =>
  import("@/pages/AuditLogsPage").then((module) => ({
    default: module.AuditLogsPage,
  })),
)
const DashboardPage = lazy(() =>
  import("@/pages/DashboardPage").then((module) => ({
    default: module.DashboardPage,
  })),
)
const ForbiddenPage = lazy(() =>
  import("@/pages/ForbiddenPage").then((module) => ({
    default: module.ForbiddenPage,
  })),
)
const LoginPage = lazy(() =>
  import("@/pages/LoginPage").then((module) => ({
    default: module.LoginPage,
  })),
)
const NotFoundPage = lazy(() =>
  import("@/pages/NotFoundPage").then((module) => ({
    default: module.NotFoundPage,
  })),
)
const OpsAssistantPage = lazy(() =>
  import("@/pages/OpsAssistantPage").then((module) => ({
    default: module.OpsAssistantPage,
  })),
)
const PermissionsPage = lazy(() =>
  import("@/pages/PermissionsPage").then((module) => ({
    default: module.PermissionsPage,
  })),
)
const PermissionsTrashPage = lazy(() =>
  import("@/pages/PermissionsTrashPage").then((module) => ({
    default: module.PermissionsTrashPage,
  })),
)
const ProfilePage = lazy(() =>
  import("@/pages/ProfilePage").then((module) => ({
    default: module.ProfilePage,
  })),
)
const RolesPage = lazy(() =>
  import("@/pages/RolesPage").then((module) => ({
    default: module.RolesPage,
  })),
)
const RolesTrashPage = lazy(() =>
  import("@/pages/RolesTrashPage").then((module) => ({
    default: module.RolesTrashPage,
  })),
)
const CmdbAssetsPage = lazy(() =>
  import("@/pages/CmdbAssetsPage").then((module) => ({
    default: module.CmdbAssetsPage,
  })),
)
const CmdbAssetsTrashPage = lazy(() =>
  import("@/pages/CmdbAssetsTrashPage").then((module) => ({
    default: module.CmdbAssetsTrashPage,
  })),
)
const DeviceCommandPoliciesPage = lazy(() =>
  import("@/pages/DeviceCommandPoliciesPage").then((module) => ({
    default: module.DeviceCommandPoliciesPage,
  })),
)
const DeviceCommandPoliciesTrashPage = lazy(() =>
  import("@/pages/DeviceCommandPoliciesTrashPage").then((module) => ({
    default: module.DeviceCommandPoliciesTrashPage,
  })),
)
const MonitorTargetsPage = lazy(() =>
  import("@/pages/MonitorTargetsPage").then((module) => ({
    default: module.MonitorTargetsPage,
  })),
)
const MonitorLogsPage = lazy(() =>
  import("@/pages/MonitorLogsPage").then((module) => ({
    default: module.MonitorLogsPage,
  })),
)
const UsersPage = lazy(() =>
  import("@/pages/UsersPage").then((module) => ({
    default: module.UsersPage,
  })),
)
const UsersTrashPage = lazy(() =>
  import("@/pages/UsersTrashPage").then((module) => ({
    default: module.UsersTrashPage,
  })),
)
const SystemConfigPage = lazy(() =>
  import("@/pages/SystemConfigPage").then((module) => ({
    default: module.SystemConfigPage,
  })),
)

export function App() {
  const { bootstrap, isInitialized } = useAuth()

  // 应用启动时用 refresh_token cookie 尝试恢复会话，避免刷新页面被误判为未登录
  useEffect(() => {
    bootstrap()
  }, [bootstrap])

  if (!isInitialized) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Spinner className="size-6 text-muted-foreground" />
      </div>
    )
  }

  return (
    <ErrorBoundary>
      <Suspense
        fallback={
          <div className="flex h-screen items-center justify-center">
            <Spinner className="size-6 text-muted-foreground" />
          </div>
        }
      >
        <Routes>
          {/* 登录页 */}
          <Route path={ROUTES.LOGIN} element={<LoginPage />} />

          {/* 受保护路由 */}
          <Route
            element={
              <ProtectedRoute>
                <AppLayout />
              </ProtectedRoute>
            }
          >
            <Route path={ROUTES.DASHBOARD} element={<DashboardPage />} />
            <Route path={ROUTES.OPS_ASSISTANT} element={<OpsAssistantPage />} />
            <Route
              path={ROUTES.USERS}
              element={
                <ProtectedRoute permission={PERMISSIONS.USER_READ}>
                  <UsersPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.USERS_TRASH}
              element={
                <ProtectedRoute permission={PERMISSIONS.USER_DELETE}>
                  <UsersTrashPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.ROLES}
              element={
                <ProtectedRoute permission={PERMISSIONS.ROLE_READ}>
                  <RolesPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.ROLES_TRASH}
              element={
                <ProtectedRoute permission={PERMISSIONS.ROLE_DELETE}>
                  <RolesTrashPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.PERMISSIONS}
              element={
                <ProtectedRoute permission={PERMISSIONS.PERMISSION_READ}>
                  <PermissionsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.PERMISSIONS_TRASH}
              element={
                <ProtectedRoute permission={PERMISSIONS.PERMISSION_DELETE}>
                  <PermissionsTrashPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.CMDB}
              element={
                <ProtectedRoute permission={PERMISSIONS.CMDB_READ}>
                  <CmdbAssetsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.CMDB_TRASH}
              element={
                <ProtectedRoute permission={PERMISSIONS.CMDB_MANAGE}>
                  <CmdbAssetsTrashPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.MONITOR_TARGETS}
              element={
                <ProtectedRoute permission={PERMISSIONS.MONITOR_READ}>
                  <MonitorTargetsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.DEVICE_COMMAND_POLICIES}
              element={
                <ProtectedRoute permission={PERMISSIONS.DEVICE_COMMAND_POLICY_READ}>
                  <DeviceCommandPoliciesPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.DEVICE_COMMAND_POLICIES_TRASH}
              element={
                <ProtectedRoute permission={PERMISSIONS.DEVICE_COMMAND_POLICY_MANAGE}>
                  <DeviceCommandPoliciesTrashPage />
                </ProtectedRoute>
              }
            />
            <Route path={ROUTES.PROFILE} element={<ProfilePage />} />
            <Route
              path={ROUTES.MONITOR_LOGS}
              element={
                <ProtectedRoute permission={PERMISSIONS.MONITOR_LOG_READ}>
                  <MonitorLogsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.AUDIT}
              element={
                <ProtectedRoute permission={PERMISSIONS.AUDIT_READ}>
                  <AuditLogsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path={ROUTES.SYSTEM_CONFIG}
              element={
                <ProtectedRoute permission={PERMISSIONS.SYSTEM_CONFIG_MANAGE}>
                  <SystemConfigPage />
                </ProtectedRoute>
              }
            />
          </Route>

          {/* 错误页面 */}
          <Route path={ROUTES.FORBIDDEN} element={<ForbiddenPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </Suspense>
    </ErrorBoundary>
  )
}

export default App
