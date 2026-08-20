# ent-agent

[English](./README.md)

基于 **FastAPI + React 19** 的企业级全栈 **AI 运维助手与 RBAC 管理平台**。

通过自然语言与 IT 基础设施直接交互：查资产拓扑、探设备健康、基于 Netmiko 执行多厂商网络命令、管理命令免审策略，以及执行严密的人机协同（HITL）变更审批流。

---

## 产品演示

<!-- 录好的动图放到 docs/images/product_demo.gif，然后把下面这行恢复出来：
![产品演示](./docs/images/product_demo.gif)
-->

一次完整的运维会话：

1. 超级管理员登录与控制台大盘；
2. CMDB 资产管理与依赖拓扑查看；
3. 设备命令策略（白名单免审/黑名单拦截）规则配置；
4. 运维助手对话：
   - 查询交换机运行配置——Agent 自行选工具、执行，并由 AI 生成摘要；
   - 执行设备重启敏感操作——**被审批闸门拦下**，提案停在 `PENDING` 等人工确认。

---

## 核心功能特性

- **🤖 智能运维助手（Ops Assistant）**：
  - 自然语言故障排查、CMDB 关联查询、实时端口 TCP 探活与交换机/路由器配置巡检。
  - 基于 Netmiko 的多厂商网络设备自动化（支持 Cisco IOS-XE、Cisco Small Business、Huawei VRP、HP Comware、Juniper Junos、Linux）。
  - WebSocket 实时流式响应、问答轮次划分、中间执行过程可折叠、长文本右上角粘性悬浮复制。
  - 支持中途中止正在跑的一轮；每条回答下方显示本轮 token 用量与花费。
  - 大模型分三档配置（便宜 / 平衡 / 强），某档未配置时整档回退到平衡档。
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
  - 状态页式可用率条：最近 1 小时、每分钟一格，附时间加权可用率——数据随列表接口一并返回，前端不逐行追加请求。
- **📚 知识库管理与 RAG**：
  - 支持 `.md` 与 `.txt` 文档上传，中英文友好分块与基于 `pgvector` 的余弦相似度向量检索。
  - 站内文档预览、AI 建议分类、删除与可恢复回收站——删除会同时让两条检索路径（向量检索与 ripgrep 文件扫描）都失效，不留半删状态。
- **🎯 Agent 效果评测（Eval）**：
  - 10 条固定用例跑**真模型**，按三层打分（结果 / 轨迹不变量 / 效率）。
  - 安全类是硬红线；能力类看相对基线的汇总成功率——因为真模型本来就会自己抖动。详见 [docs/EVAL.md](./docs/EVAL.md)。
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
| **质量基线** | Ruff、mypy（严格模式）、pytest（1120 用例） / ESLint、Prettier、Vitest（169 用例） |

---

## 仓库结构

```text
ent-agent/
├── backend/          # FastAPI 后端服务（详见 backend/README_zh.md）
│   └── evals/        # Agent 效果评测套件——10 条用例跑真模型
├── frontend/         # React SPA 前端应用（详见 frontend/README_zh.md）
├── docs/             # PRD、架构设计、部署规范、评测设计与时序图
├── README.md         # 英文说明
└── README_zh.md      # 中文说明（本文件）
```

---

## 快速开始（Docker，推荐）

整套系统（PostgreSQL + 后端 + 前端）都在容器里跑，本机只需要装 Docker。

```bash
cp backend/.env.example backend/.env   # 填入模型密钥、INIT_SUPERUSER_* 等
docker compose up -d --build
```

打开 <http://localhost:8090>。前端与 API **同源**：nginx 把 `/api` 反代到后端，
所以构建产物里不含任何后端地址，同一份镜像可以部署到任意域名。

启动时后端会自动 `alembic upgrade head` 并跑一次幂等的种子初始化，不需要手工执行。

```bash
docker compose logs -f backend   # 看迁移、种子与运行日志
docker compose down              # 停止（数据卷保留）
docker compose down -v           # 连同数据一起删除，慎用
```

几个刻意的选择：

- **端口 8090 而不是 8080**：`LLM_CHAT_BASE_URL` 默认指向 `127.0.0.1:8080` 的本地模型服务，避开它。
- **后端不映射宿主端口**：只经 nginx 同源访问，避免出现"两个后端地址"的困惑。
- **镜像里装了 ripgrep**：`kb_grep` 工具直接调 `rg`，缺了它知识库全文检索会在运行时失败。
- **只跑单个 worker**：Agent 的 Spawn 运行时是进程内状态，`WEB_CONCURRENCY` 只能是 1。
- 知识库正文与回收站挂在具名卷上，重建镜像不会丢已上传的文档。

## 快速开始（本地运行）

想改代码、用热重载时更方便。

**前置要求**

- **Python 3.14** 与 [uv](https://github.com/astral-sh/uv)
- **Node.js 24**（npm）
- **PostgreSQL ≥ 14**，需启用 `pgvector` 与 `pg_trgm` 扩展
  （可以只起编排里的数据库：`docker compose up -d postgres`，扩展已包含）
- **ripgrep**——`kb_grep` 工具直接调 `rg`
  （`winget install BurntSushi.ripgrep.MSVC` / `brew install ripgrep` / `apt install ripgrep`）
- 本地 Embedding 模型（如 llama.cpp）或任意 OpenAI 兼容大模型 API

```bash
# 后端
cd backend
cp .env.example .env      # 配置 DATABASE_URL、CMDB_CREDENTIAL_KEY 与 LLM_CHAT_* / LLM_EMBEDDING_*
uv sync
uv run alembic upgrade head
uv run python init_db.py
uv run uvicorn app.main:app --reload

# 前端（另开一个终端）
cd frontend
npm install
npm run dev
```

| | |
| --- | --- |
| Web 前端 | `http://localhost:5173` |
| 接口前缀 | `http://localhost:8000/api/v1` |
| OpenAPI 文档 | `http://localhost:8000/docs` |

使用 `init_db.py` 初始化的超级管理员账号登录（`admin` / `admin123`）。
**在把实例暴露给其他人之前先改掉它。**

> Windows 上改用 `uv run python main.py` 启动后端——它会选用异步 psycopg 所需的
> Selector 事件循环。

---

## 架构与技术文档

| 文档 | 说明 |
| --- | --- |
| [docs/AGENT_ARCHITECTURE.md](./docs/AGENT_ARCHITECTURE.md) | Agent 平台架构设计与 WebSocket 契约规范 |
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) | 后端分层架构与数据库设计 |
| [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) | 生产环境部署与安全加固指南 |
| [docs/SYSTEM_CONFIG.md](./docs/SYSTEM_CONFIG.md) | 系统运行配置、密钥管理与优先级规范 |
| [docs/EVAL.md](./docs/EVAL.md) | Agent 效果评测套件——用真模型做防回归 |
| [backend/README_zh.md](./backend/README_zh.md) | 后端开发指南（[English](./backend/README.md)） |
| [frontend/README_zh.md](./frontend/README_zh.md) | 前端开发指南（[English](./frontend/README.md)） |

---

## 许可证

[MIT](./LICENSE) © lijianqiao
