# ent-agent

[English](./README.md)

基于 **FastAPI + React 19** 的企业级全栈 **AI 运维助手与 RBAC 管理平台**。

通过自然语言与 IT 基础设施直接交互：查资产拓扑、探设备健康、基于 Netmiko 执行多厂商网络命令、管理命令免审策略，以及执行严密的人机协同（HITL）变更审批流。

---

## 产品演示

![产品演示](./docs/images/product_demo.gif)

演示包含：
1. 超级管理员登录与控制台大盘；
2. CMDB 资产管理与依赖拓扑查看；
3. 设备命令策略（白名单免审/黑名单拦截）规则配置；
4. 运维助手对话：
   - 查询交换机运行配置（触发自动化执行并由 AI 生成摘要）；
   - 执行设备重启敏感操作（命中黑名单策略拦截与安全防护提示）。

---

## 核心功能特性

- **🤖 智能运维助手（Ops Assistant）**：
  - 自然语言故障排查、CMDB 关联查询、实时端口 TCP 探活与交换机/路由器配置巡检。
  - 基于 Netmiko 的多厂商网络设备自动化（支持 Cisco IOS-XE、Cisco Small Business、Huawei VRP、HP Comware、Juniper Junos、Linux）。
  - WebSocket 实时流式响应、问答轮次划分、中间执行过程可折叠、长文本右上角粘性悬浮复制。
- **🛡️ 人机协同安全闸门（HITL）**：
  - 针对变更操作提供 `PENDING → APPROVED → EXECUTING → EXECUTED / UNKNOWN` 状态机闭环。
  - 待审批全局自动弹出模态窗口，支持 6 位 `InputOTP` 动态凭据分格口令输入，审批提交后即刻关闭。
  - 针对万行设备配置大输出，提供隔离存储与幂等 AI 异步配置总结。
- **🗄️ CMDB 资产与凭据管理**：
  - 集中管理交换机、路由器、服务器资产信息、子网 CIDR 与有向依赖拓扑图。
  - 静态密码基于 Fernet 对称加密（`CMDB_CREDENTIAL_KEY`）落库存储，并支持动态口令接入。
- **⚡ 设备命令免审批策略**：
  - 按资产类型或具体设备配置白名单/黑名单策略，精准控制免审批与强管控边界。
- **📡 TCP 监控探活与实时告警**：
  - 异步并发 TCP 端口探活扫描，跟踪在线状态与延迟，状态翻转时向前端广播实时告警横幅。
- **📚 知识库管理与 RAG**：
  - 支持 `.md` 与 `.txt` 文档上传，中英文友好分块与基于 `pgvector` 的余弦相似度向量检索。
- **🔐 企业级 RBAC 权限体系**：
  - 用户 ↔ 角色 ↔ 权限三层模型，模块化权限码（`user:read`、`agent:hitl_approve` 等），支持软删除与回收站。
  - 内存短期 Access Token + HttpOnly Refresh Cookie 会话族轮换与防重放撤销。
- **⚙️ 运行时动态配置**：
  - 支持在 UI 中动态配置 LLM/Embedding 模型厂商、API Key、价格及探活参数，优先于 `.env`。

---

## 技术栈

| 层级 | 技术选型 |
| --- | --- |
| **后端** | Python 3.14、FastAPI、SQLAlchemy 2（异步）、PostgreSQL + pgvector、Alembic、Netmiko、JWT、uv |
| **前端** | React 19、TypeScript、Vite 8、Tailwind CSS 4、shadcn/ui（Base UI）、Zustand、React Router 7 |
| **质量基线** | Ruff、mypy（严格模式）、pytest（955+ 用例） / ESLint、Prettier、Vitest（134+ 用例） |

---

## 仓库结构

```text
ent-agent/
├── backend/          # FastAPI 后端服务（详见 backend/README_zh.md）
├── frontend/         # React SPA 前端应用（详见 frontend/README_zh.md）
├── docs/             # PRD、架构设计、部署规范、时序图与演示动图
├── README.md         # 英文说明
└── README_zh.md      # 中文说明（本文件）
```

---

## 快速开始

### 前置要求

- **Python 3.14** 与 [uv](https://github.com/astral-sh/uv)
- **Node.js ≥ 20**（npm 或 pnpm）
- **PostgreSQL ≥ 14**（建议启用 `pgvector` 与 `pg_trgm` 扩展）
- 本地 Embedding 模型（如 llama.cpp）或任意 OpenAI 兼容大模型 API

### 1. 后端启动

```bash
cd backend
cp .env.example .env

# 配置 DATABASE_URL、CMDB_CREDENTIAL_KEY 以及 LLM 模型参数
uv sync
uv run alembic upgrade head
uv run python init_db.py
uv run python main.py
```

- 接口前缀：`http://localhost:8000/api/v1`
- OpenAPI 文档：`http://localhost:8000/docs`

### 2. 前端启动

```bash
cd frontend
cp .env.example .env
npm install
npm run dev
```

- Web 前端：`http://localhost:5173`

使用 `init_db.py` 初始化的默认超级管理员账号登录（`admin` / `admin123`）。

---

## 架构与技术文档

| 文档 | 说明 |
| --- | --- |
| [docs/AGENT_ARCHITECTURE.md](./docs/AGENT_ARCHITECTURE.md) | Agent 平台架构设计与 WebSocket 契约规范 |
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) | 后端分层架构与数据库设计 |
| [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) | 生产环境部署与安全加固指南 |
| [docs/SYSTEM_CONFIG.md](./docs/SYSTEM_CONFIG.md) | 系统运行配置、密钥管理与优先级规范 |
| [backend/README_zh.md](./backend/README_zh.md) | 后端开发指南（[English](./backend/README.md)） |
| [frontend/README_zh.md](./frontend/README_zh.md) | 前端开发指南（[English](./frontend/README.md)） |

---

## 许可证

[MIT](./LICENSE) © lijianqiao
