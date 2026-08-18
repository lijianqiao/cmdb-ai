# 前端 — ent-agent

[English](./README.md) · [根目录说明](../README_zh.md)

**ent-agent** 平台的现代 **React 19 单页应用（SPA）** 前端项目。基于 **TypeScript**、**Vite 8**、**Tailwind CSS 4** 与 **shadcn/ui（Base UI）** 构建。

---

## 架构总览

详细架构契约见 [docs/AGENT_ARCHITECTURE.md](../docs/AGENT_ARCHITECTURE.md)。

```text
frontend/
├── src/
│   ├── components/
│   │   ├── auth/             # ProtectedRoute 权限与登录态路由守卫
│   │   ├── cmdb/             # CMDB 资产表单弹窗、厂商选择器与依赖管理
│   │   ├── common/           # DataTable（TanStack Table 数据表格）、确认弹窗、分页器、错误边界
│   │   ├── device-command-policies/ # 命令白名单/黑名单免审策略表单
│   │   ├── layout/           # AppLayout 布局、侧边栏导航、带明暗主题切换的 Header、PageHeader
│   │   ├── monitor/          # 监控目标与探活参数表单及校验 Schema
│   │   ├── ops-assistant/    # 【核心】运维助手 AI 对话组件树、问答轮次列表、HITL 弹窗与审批卡片
│   │   ├── permissions/      # 权限管理弹窗组件
│   │   ├── roles/            # 角色管理与权限分配弹窗组件
│   │   ├── system-config/    # LLM/Embedding 大模型与运维运行参数配置卡片
│   │   ├── ui/               # 32 个 shadcn/ui（Base UI）原子级基础组件
│   │   └── users/            # 用户增删改查、角色分配与密码重置弹窗
│   ├── hooks/                # 核心自定义 Hook（useOpsChat、useAgentWs、useAuth、usePermission 等）
│   ├── lib/                  # Axios HTTP 客户端、WebSocket 纯函数、API 请求模块、常量定义、Hugeicons 图标
│   ├── pages/                # 页面级视图组件（100% 通过 React.lazy 实现代码分割懒加载）
│   ├── store/                # Zustand 内存级认证状态管理
│   ├── types/                # 严格的 TypeScript 类型定义（Agent、CMDB、Auth、Monitor、权限等）
│   ├── App.tsx               # 应用根组件、启动时会话自动恢复（bootstrap）与路由树注册
│   ├── index.css             # Tailwind CSS 4 主题变量与全局样式
│   └── main.tsx              # React 挂载入口，注入 ThemeProvider、TooltipProvider 与 Sonner Toast
├── vite.config.ts            # Vite 8 配置文件（含开发态 `/api` 与 WebSocket 代理）
├── package.json              # 依赖与脚本声明
└── tsconfig.json             # 严格 TypeScript 编译选项
```

---

## 运维助手交互与组件架构 (`components/ops-assistant/`)

运维助手交互体验针对多轮 AI 推理、工具透明执行与人机协同安全审批进行了系统化设计：

1. **问答轮次时间线 (`ChatMessageList.tsx`)**：
   - 将 WebSocket 扁平事件流归组为以用户提问划分的结构化 `Turn` 轮次；
   - 每一轮由三部分组成：(1) 超长自动折叠的用户提问气泡、(2) 可折叠的中间思考与执行过程、(3) 助手最终 Markdown 回答；
   - 用户提问与最终回答均配置**粘性悬浮复制图标**（`CopyButton.tsx`），向下滚动长内容时始终吸附在右上角可视区域。
2. **中间思考与执行过程折叠面板 (`ExecutionProcessCollapsible.tsx`)**：
   - 聚合呈现模型思考文本、`tool_call` 工具调用徽标、子 Agent 运行卡片以及 HITL 审批卡片；
   - 生成中或等待审批时默认展开展示实时动态；最终回答生成完毕后自动折叠，保持页面清爽。
3. **全局主动审批弹窗 (`HitlApprovalDialog.tsx`)**：
   - 当收到 `PENDING` 审批提案时，在屏幕中央主动弹出模态窗口；
   - 针对动态凭据设备，集成官方 **Shadcn `InputOTP`** 6 位分格口令输入框，支持密码掩码与自动聚焦；
   - 点击“批准并执行”或“拒绝”后立即关闭弹窗，不阻塞后台设备执行与后续流式输出。
4. **时间线审批卡片 (`HitlApprovalCard.tsx`)**：
   - 在时间线内常驻保留精简审批记录；
   - 针对 `device_query` 查询大文本配置，提供抽屉展开查看完整回显，并支持一键**恢复 AI 总结**。
5. **子 Agent 状态卡片 (`ChildAgentStatusCard.tsx`)**：
   - 实时展示动态 Spawn 的子任务角色、简述、进度状态徽标（`RUNNING`/`COMPLETED`/`FAILED`）与结果回执。
6. **流式富文本渲染 (`ChatMarkdown.tsx`)**：
   - 定制 GFM Markdown 渲染器，针对代码块、表格、引用与状态标签进行视觉调优。

---

## 状态管理与网络通信

- **`use-ops-chat.ts`**：核心对话状态管理 Hook。切换会话时，先通过 REST API 拉取完整快照，待数据稳定后再建立 WebSocket 连接，彻底规避并发数据覆盖竞态；通过纯 Reducer 合并增量流。
- **`use-agent-ws.ts`**：管理 WebSocket 生命周期，携带内存 Access Token 进行鉴权，断线采用指数退避算法（1s–30s）自动重连。
- **`use-auth.ts` 与 `store/auth.ts`**：双 Token 架构。Access Token 纯内存存放；页面刷新时由 `bootstrap()` 通过 HttpOnly Cookie 自动换取新 Token 恢复登录态，杜绝页面闪烁。
- **`use-permission.ts`**：细粒度 RBAC 权限判断（`hasPermission` 等），超级管理员直接放行所有功能。
- **`src/lib/api.ts`**：Axios 全局实例，内置 401 拦截器与并发请求队列，支持无感静默刷新 Token 并重放失败请求。

---

## 页面路由与权限映射

所有业务视图均通过 `React.lazy()` 独立打包与按需加载：

| 访问路径 | 对应组件 | 页面功能 | 所需权限 |
| --- | --- | --- | --- |
| `/login` | `LoginPage` | 账号登录界面 | 公开访问 |
| `/` | `DashboardPage` | 控制台运行大盘与汇总指标 | 需登录 |
| `/ops-assistant` | `OpsAssistantPage` | 运维助手智能对话与审批处理 | 需登录 (`agent:use`) |
| `/cmdb` | `CmdbAssetsPage` | CMDB 资产列表与上下游依赖拓扑图 | `cmdb:read` |
| `/cmdb/trash` | `CmdbAssetsTrashPage` | 已软删除资产回收站 | `cmdb:manage` |
| `/device-command-policies` | `DeviceCommandPoliciesPage` | 设备命令白名单/黑名单免审批策略 | `device_command_policy:read` |
| `/monitor-targets` | `MonitorTargetsPage` | TCP 探活监控目标与实时延迟 | `monitor:read` |
| `/monitor-logs` | `MonitorLogsPage` | 探活健康事件历史日志 | `monitor_log:read` |
| `/users` | `UsersPage` | 用户列表、角色分配与重置密码 | `user:read` |
| `/roles` | `RolesPage` | 角色管理与权限绑定配置 | `role:read` |
| `/permissions` | `PermissionsPage` | 系统模块权限树与权限点管理 | `permission:read` |
| `/system-config` | `SystemConfigPage` | 大模型厂商、费率与运维探活运行配置 | `system_config:manage` |
| `/audit` | `AuditLogsPage` | 系统审计操作日志多维查询 | `audit:read` |
| `/profile` | `ProfilePage` | 个人资料与修改登录密码 | 需登录 |

---

## Docker 运行（推荐）

在仓库根目录 `docker compose up -d --build`，然后访问 <http://localhost:8090>。

前端镜像是「Vite 构建产物 + nginx」，nginx 同时负责：

- 把 `/api` 反代到后端，**包含 WebSocket 升级**（运维助手的实时事件走
  `/api/v1/ws/agent/{id}`，少了升级头会表现为"页面能开、就是不推消息"）；
- SPA 路由回退，`/knowledge`、`/users/trash` 这类深链接刷新不会 404；
- 把代理读超时放宽到 600s，一轮多步工具调用的长回答不会被拦腰截断。

因为 `src/lib/api.ts` 的 `baseURL` 默认是相对路径 `/api/v1`、WS 地址由页面 host 推出，
构建产物里不含任何后端地址，同一份镜像可部署到任意域名，不需要按环境重新构建。

## 本地启动与开发

### 安装依赖

```bash
cd frontend
cp .env.example .env

# 安装依赖
npm install  # 或 pnpm install
```

### 启动开发服务器

```bash
npm run dev
```

浏览器访问 `http://localhost:5173`。Vite 会将 `/api` 请求及 WebSocket 连接透明代理至后端的 `http://127.0.0.1:8000`。

---

## 脚本与质量基线

```bash
# 启动 Vite 开发调试服务
npm run dev

# 运行全量 Vitest 单元测试（134+ 用例全部通过）
npm test

# 执行严格 TypeScript 类型检查
npm run typecheck

# 代码格式规范与 ESLint 检查
npm run lint

# 生产环境打包构建（主入口 chunk 经优化 < 500KB）
npm run build

# 本地预览生产构建产物
npm run preview
```

---

## 许可证

[MIT](../LICENSE) © lijianqiao
