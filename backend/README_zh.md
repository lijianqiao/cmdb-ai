# 后端 — ent-agent

[English](./README.md) · [根目录说明](../README_zh.md)

**ent-agent** 平台的异步 **FastAPI** 后端服务。提供企业级 RBAC 权限管理、CMDB 资产与拓扑依赖、TCP 探活监控、Netmiko 多厂商网络设备自动化，以及支持人机协同（HITL）安全闸门的多轮 LLM 运维 Agent 运行时。

---

## 架构总览

详细架构规范见 [docs/AGENT_ARCHITECTURE.md](../docs/AGENT_ARCHITECTURE.md) 与 [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)。

```text
backend/
├── app/
│   ├── api/v1/              # HTTP 接口与 WebSocket 路由（auth、agent、cmdb、monitor、hitl 等）
│   ├── agent/               # Agent 运行时：循环、单轮编排、WS Hub、HITL 门控、工具分派、Netmiko 执行器
│   ├── core/                # 全局配置、异步 DB 连接、安全认证（Argon2id/JWT）、Fernet 密文、LLM 客户端
│   ├── crud/                # 数据访问层（SQLAlchemy 2 异步查询与软删除支持）
│   ├── models/              # Declarative ORM 数据模型（PostgreSQL + pgvector）
│   ├── schemas/             # Pydantic 请求/响应校验模型与 WS 事件契约
│   ├── services/            # 核心业务服务：Token 会话轮换、TCP 探活扫描、CMDB 差异巡检、知识库切片入库
│   ├── utils/               # 统一操作审计日志辅助函数
│   └── main.py              # FastAPI ASGI 应用工厂、中间件、异常处理与 Lifespan 生命周期
├── alembic/                 # 数据库版本迁移脚本
├── knowledge/               # 知识库上传物理文件隔离存储目录
├── tests/                   # 955+ 自动化测试用例（覆盖率高，持续集成）
├── init_db.py               # 超管账号初始化与 24 项系统权限种子
├── main.py                  # Windows 友好的 Uvicorn 启动入口（配置 SelectorEventLoop）
├── pyproject.toml           # 基于 uv 的依赖与工具链配置
└── .env.example             # 环境变量配置模板
```

---

## 核心系统与模块

### 1. Agent 核心运行时 (`app/agent/`)
- **执行主循环 (`loop.py`)**：标准多轮工具调用循环，具备步数上限（`max_steps`）与费用硬上限（`max_cost_usd`）双重预算保护。
- **单轮对话编排 (`chat_turn.py`)**：运维助手主干。组装 System Prompt，通过 SSE 消费大模型流式 Token，分派工具调用，并通过 WebSocket 实时广播执行状态。
- **上下文智能压缩 (`compaction.py`)**：当上下文 Token 超标时，自动在后台调用轻量 LLM 将旧窗口历史消息总结为 `memory_summary`，防止 Prompt 溢出并留存关键事实。
- **人机协同审批闸门 (`hitl_gate.py` 与 `hitl_execution.py`)**：拦截状态变更与敏感操作（`notify`、`device_control`、`query_device_command`），驱动 `PENDING → APPROVED → EXECUTING → EXECUTED / UNKNOWN` 状态机流转。
- **Netmiko 网络执行器 (`executors.py`)**：支持思科（IOS-XE、Small Business SG350X）、华为、华三、Juniper、Linux 等设备的 SSH/CLI 命令交互。
- **子 Agent 体系 (`spawn.py` 与 `orchestration.py`)**：进程内动态管理只读子任务生命周期，提供文档批量分类（`classify_documents`）与多假设根因排查（`investigate_root_cause`）确定性工作流。
- **WebSocket 广播中心 (`ws_hub.py`)**：向前端客户端推送安全脱敏的事件流（`assistant_delta`、`tool_call`、`hitl_pending`、`monitor_alert` 等）。

### 2. 基础设施与监控运维
- **CMDB 资产与拓扑依赖 (`app/crud/cmdb_asset*.py`)**：管理 IT 资产软硬件信息、IP、所属业务系统与有向依赖拓扑图。设备静态凭据由 `CMDB_CREDENTIAL_KEY`（Fernet）高强度对称加密存储。
- **设备命令策略引擎 (`app/crud/device_command_policy.py`)**：按资产类型或单台设备建立白名单/黑名单，实现免审批与强拦截规则引擎。
- **TCP 监控探活扫描 (`app/services/monitor_sweep.py`)**：后台高并发异步探活扫描，计算延迟并记录状态事件。当目标状态翻转（up↔down）时自动触发全量告警广播。
- **知识库 RAG (`app/services/knowledge_ingestion.py`)**：文档 SHA-256 去重、CJK 优化分块、向量化并通过 PostgreSQL `pgvector` 进行余弦相似度检索。

### 3. 认证安全体系 (`app/core/` 与 `app/services/auth.py`)
- **双 Token 无感刷新**：内存保存短期 Access Token，HttpOnly Cookie 携带 Refresh Token。
- **会话族防重放**：每次使用 Refresh Token 均轮换 JTI；一旦检测到已作废 Token 被重用，立刻整族吊销以防凭据失窃。
- **现代密码哈希**：基于 `pwdlib` 的 Argon2id 算法，并支持旧版本 Bcrypt 哈希的自动平滑迁移。
- **请求来源强校验**：对所有非幂等操作严格校验 Origin/Referer 头部，防御 CSRF 攻击。

---

## 主要 API 路由概览 (`/api/v1`)

| 路由前缀 | 请求方法 / 作用范围 | 功能说明 | 所需权限 |
| --- | --- | --- | --- |
| `/auth` | POST | 注册、登录、Token 轮换刷新、注销退出 | 公开 / 需登录 |
| `/me` | GET, PATCH, PUT | 个人资料查看、修改昵称邮箱、修改登录密码 | 需登录 |
| `/users` | CRUD, 回收站, 密码重置 | 用户账号生命周期与角色分配 | `user:*` |
| `/roles` | CRUD, 回收站, 权限分配 | 角色管理与权限绑定 | `role:*` |
| `/permissions` | GET, CRUD | 树形权限展示与权限点管理 | `permission:*` |
| `/cmdb` | CRUD, 拓扑关系 | 资产增删改查、上下游依赖图、凭据预览 | `cmdb:*` |
| `/device-command-policies` | CRUD, 回收站 | 设备命令白名单/黑名单免审批策略 | `device_command_policy:*` |
| `/monitor` | GET, CRUD | 监控目标配置、实时健康状态与探活日志 | `monitor:*` |
| `/knowledge` | GET, POST | 知识分类创建与 Markdown/TXT 文档向量上传 | `knowledge:*` |
| `/hitl` | GET, POST | 待审批提案、批准/拒绝决策、重试执行、处置 | `agent:hitl_approve` |
| `/agent` | CRUD, POST | 会话管理、快照安全恢复、发送用户提问 | `agent:use` |
| `/ws/agent/{session_id}` | WebSocket | 实时 Token 流推送与状态事件广播 | `agent:use` |
| `/system-config` | GET, PUT | 运行时动态大模型、费率与监控参数配置 | `system_config:*` |
| `/audit-logs` | GET | 全局操作与系统审计日志多维检索 | `audit:read` |

---

## 本地安装与启动

### 环境准备
- **Python 3.14**
- [uv](https://github.com/astral-sh/uv)
- **PostgreSQL ≥ 14**（需已启用 `pgvector` 与 `pg_trgm` 扩展）

### 启动步骤

```bash
cd backend
cp .env.example .env

# 使用 uv 同步安装依赖
uv sync

# 执行数据库版本迁移
uv run alembic upgrade head

# 写入超管账号与 24 项权限种子
uv run python init_db.py

# 启动服务
uv run python main.py
```

- API 基础地址：`http://localhost:8000/api/v1`
- Swagger API 文档：`http://localhost:8000/docs`

---

## 质量基线与测试

后端严格执行零警告/零错误质量基线：

```bash
# 格式化与代码风格规范检查
uv run ruff check .

# 严格静态类型检查（覆盖全部模块 120+ 文件）
uv run mypy app

# 运行全量单元与集成测试（955+ 用例全部通过）
uv run pytest
```

---

## 许可证

[MIT](../LICENSE) © lijianqiao
