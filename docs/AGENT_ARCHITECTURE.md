# 运维 Agent 平台 — 架构设计文档

> **项目名称**: ent-agent(基于 fastapi-admin RBAC 基座的二次开发)
> **文档版本**: v1.0
> **撰写人**: 李剑桥
> **日期**: 2026-08-10
> **基于**: [docs/ARCHITECTURE.md](./ARCHITECTURE.md)（RBAC 基座 v1.1）+ [docs/guide.md](./guide.md)（Agent 开发范式手册 2026-08，本文档不修改该文件，仅作为技术路线参照）

---

## 目录

- [Part A: 系统设计](#part-a-系统设计)
  - [1. 需求背景与产品定位](#1-需求背景与产品定位)
  - [2. 总体架构](#2-总体架构)
  - [3. 数据模型](#3-数据模型)
  - [4. Agent 角色目录与工具契约](#4-agent-角色目录与工具契约)
  - [5. 动态 Spawn 编排](#5-动态-spawn-编排)
  - [6. HITL 状态机](#6-hitl-状态机)
  - [7. 确定性管道](#7-确定性管道)
  - [8. 会话、压缩与前端实时通道](#8-会话压缩与前端实时通道)
  - [9. 安全基线映射（L0–L6）](#9-安全基线映射l0l6)
  - [10. 可观测性与预算](#10-可观测性与预算)
  - [11. 待明确事项与假设](#11-待明确事项与假设)
- [Part B: 任务分解](#part-b-任务分解)
  - [12. 新增依赖](#12-新增依赖)
  - [13. 任务列表（延续 T01–T05 编号）](#13-任务列表延续-t01t05-编号)
  - [14. 任务依赖图](#14-任务依赖图)

---

## Part A: 系统设计

### 1. 需求背景与产品定位

在现有 RBAC 后台（[ARCHITECTURE.md](./ARCHITECTURE.md)）之上，新增一个**统一运维助手**：一个 Chat 页面，融合三类能力——

1. **运维知识问答**：上传 SOP/故障处理手册/网络拓扑说明/厂商手册，Agent 用混合检索（Grep 优先，向量补充）回答
2. **设备/网段在线状态查询**：基于常驻后台探活管道的实时数据回答
3. **CMDB 关联查询与根因排查**：把"资产归属/依赖关系"和"实时状态"接起来，回答需要综合判断的问题

产品形态与技术路线取舍已在讨论中确定，不再展开决策过程，直接进入设计。核心取向对齐 [guide.md 1.4](./guide.md#14-2026-共识取向)：

- **简单查询走单体 Agent 循环**（一次工具调用），**只有需要并行取证/独立上下文降噪时才 spawn**（guide.md 7.1、7.8 反模式）
- 网段探活、CMDB 差异巡检是**确定性管道**，不是 Agent（guide.md 1.3）
- Agent 只在"融合多路数据源做判断"和"问答"这两类事情上出现

### 2. 总体架构

```mermaid
graph TB
    subgraph Client["前端（新增）"]
        Chat[OpsAssistantPage<br/>单一 Chat 页面]
    end

    subgraph API["FastAPI（复用现有 app/，新增 app/agent/）"]
        WS["WebSocket 路由<br/>/api/v1/ws/agent/{session_id}"]
        Loop["Agent Loop<br/>app/agent/loop.py"]
        Dispatch["工具调度器<br/>app/agent/tools/"]
        Spawn["Spawn 编排器<br/>app/agent/spawn.py"]
        HITL["HITL 状态机<br/>app/agent/hitl.py"]
    end

    subgraph Deterministic["确定性核心（新增后台任务，同进程 asyncio）"]
        Sweep["monitor_sweep<br/>周期探活循环"]
        CmdbDiff["cmdb_diff_job<br/>差异巡检"]
    end

    subgraph Storage["存储（复用现有 PostgreSQL）"]
        PG[("PostgreSQL<br/>+ pgvector 扩展")]
        FS["knowledge/ 文件目录<br/>新增，Grep/Read 直接操作"]
    end

    subgraph ModelLayer["模型层（新增）"]
        LLM["app/core/llm.py<br/>MODELS 登记表"]
        Embed["llama.cpp 本地部署<br/>Qwen3-Embedding-0.6B + Reranker"]
    end

    Chat <-->|WebSocket 单连接| WS
    WS --> Loop
    Loop --> Dispatch
    Dispatch -->|知识检索| FS
    Dispatch -->|结构化查询| PG
    Dispatch -->|复杂任务| Spawn
    Dispatch -->|写操作提案| HITL
    Spawn -->|子 Agent 并行复用同一套工具| Dispatch
    Loop --> LLM
    LLM --> Embed
    Sweep --> PG
    CmdbDiff --> PG
    Sweep -.->|状态变化立即推送| WS
    HITL -.->|提案/审批结果推送| WS
```

**分层约束**（延续 [ARCHITECTURE.md 8.1](./ARCHITECTURE.md#81-后端编码规范) 的"禁止跨层调用"）：`app/agent/` 内的工具实现只能调用 `app/crud/`，不得绕过 CRUD 层直接拼 SQL；`app/agent/` 与 `app/api/`、`app/crud/`、`app/models/`、`app/services/` 平级，是新增的第五个顶层分层。

**不引入新中间件**：不加 Redis/消息队列，不加独立向量库，pgvector 作为 Postgres 扩展开启；子 Agent 并发用进程内 `asyncio` 任务实现（后续需要跨进程扩容时，可替换执行器实现，注册表接口不变）。

### 3. 数据模型

```mermaid
classDiagram
    direction TB

    %% ===== 知识库子系统 =====
    class KnowledgeCategory {
        +int id
        +str code
        +str name
        +str description
        +datetime created_at
    }
    class KnowledgeDocument {
        +int id
        +int category_id
        +str title
        +str original_filename
        +str file_path
        +str file_type
        +str content_hash
        +str status
        +int uploaded_by
        +bool is_deleted
        +datetime created_at
        +datetime updated_at
    }
    class KnowledgeChunk {
        +int id
        +int document_id
        +int chunk_index
        +str content
        +int token_count
        +vector~1024~ embedding
        +datetime created_at
    }
    KnowledgeCategory "1" --o "*" KnowledgeDocument : category_id
    KnowledgeDocument "1" --o "*" KnowledgeChunk : document_id

    %% ===== CMDB 子系统 =====
    class CmdbAsset {
        +int id
        +str asset_type
        +str hostname
        +str ip_address
        +str location
        +int owner_user_id
        +str business_system
        +str subnet_cidr
        +str notes
        +bool is_deleted
        +datetime created_at
        +datetime updated_at
    }
    class CmdbAssetDependency {
        +int parent_asset_id
        +int child_asset_id
        +str relation_type
        +datetime created_at
    }
    CmdbAsset "1" --o "*" CmdbAssetDependency : parent_asset_id
    CmdbAsset "1" --o "*" CmdbAssetDependency : child_asset_id

    %% ===== 监控子系统 =====
    class MonitorTarget {
        +int id
        +int cmdb_asset_id
        +str ip_address
        +int port
        +str label
        +int check_interval_seconds
        +bool is_active
        +datetime created_at
    }
    class MonitorStatusEvent {
        +int id
        +int target_id
        +str status
        +int latency_ms
        +str detail
        +datetime checked_at
    }
    CmdbAsset "1" --o "*" MonitorTarget : cmdb_asset_id
    MonitorTarget "1" --o "*" MonitorStatusEvent : target_id

    %% ===== Agent 运行时子系统 =====
    class AgentSession {
        +int id
        +int user_id
        +str title
        +str status
        +datetime created_at
        +datetime updated_at
    }
    class AgentMessage {
        +int id
        +int session_id
        +str role
        +str content
        +str tool_call_id
        +datetime created_at
    }
    class AgentRegistry {
        +str child_id
        +int session_id
        +str parent_agent_id
        +str agent_path
        +str role
        +str model
        +json tools_allowlist
        +str sandbox_mode
        +str task_brief
        +json budget
        +str status
        +str result_summary
        +json artifacts
        +datetime created_at
        +datetime closed_at
    }
    class HitlProposal {
        +int id
        +int session_id
        +str proposed_by_agent_id
        +str action_type
        +json action_payload
        +str status
        +int reviewed_by_user_id
        +datetime reviewed_at
        +datetime executed_at
        +datetime created_at
    }
    class AgentTraceEvent {
        +int id
        +str trace_id
        +int session_id
        +str agent_id
        +str parent_agent_id
        +int step
        +str span_type
        +str tool
        +str control
        +float cost_usd
        +int latency_ms
        +str error_class
        +datetime created_at
    }
    AgentSession "1" --o "*" AgentMessage : session_id
    AgentSession "1" --o "*" AgentRegistry : session_id
    AgentSession "1" --o "*" HitlProposal : session_id
    AgentSession "1" --o "*" AgentTraceEvent : session_id
```

**设计说明：**

| 决定 | 理由 |
| :--- | :--- |
| `KnowledgeDocument` 只存元数据，正文落盘到 `knowledge/{category_code}/{doc_id}_{filename}` | 对齐 [guide.md 4.3](./guide.md#43-知识落盘纪律)；`kb_grep`/`kb_glob`/`kb_read` 直接操作文件系统 |
| `KnowledgeChunk.embedding` 用 `vector(1024)`（pgvector） | 对应本地 llama.cpp 部署的 Qwen3-Embedding-0.6B 原生输出维度；实测不符可调整列定义 |
| `CmdbAssetDependency` 只有外键 + `created_at`，无自增 id | 沿用 [ARCHITECTURE.md 8.4](./ARCHITECTURE.md#84-数据库约定) 关联表约定（同 `UserRole`/`RolePermission`） |
| 不单独建"当前状态"可变字段，设备在线状态永远从 `MonitorStatusEvent` 最新一条**派生**（`DISTINCT ON (target_id) ORDER BY checked_at DESC`） | 避免"事件表"和"当前状态表"两份状态互相漂移，对应 [guide.md 3.1](./guide.md#31-确定性核心-vs-模型边缘)"审计字段代码派生"原则 |
| CMDB 变更记录、知识文档归类结果、CMDB↔监控差异巡检发现，**全部复用现有 `audit_logs` 表**，不新建审计表 | 现有 `utils.audit.log_audit()` 已经是通用工具；新增 `action` 枚举值即可，避免重复造轮子 |
| `AgentRegistry` 就是 [guide.md 7.4](./guide.md#74-childreceipt每次-spawn-必有回执) 的 `ChildReceipt` 落地为表 | 注册表必须能在压缩/断线后独立查询，不依赖对话正文（guide.md 6.3） |
| `AgentTraceEvent` 字段直接照抄 [guide.md 8.3](./guide.md#83-日志字段建议) | 保证"卡在哪一步"可回答 |

### 4. Agent 角色目录与工具契约

#### 4.1 角色目录

| 角色 | 模型档位 | 沙箱 | 触发场景 |
| :--- | :--- | :--- | :--- |
| `root`（主循环，非子 Agent） | 对话模型（登记表可配） | read-only + 可发起 HITL 提案 | 面向用户，常规问答；判断是否需要 spawn |
| `classifier` | 快/便宜 | read-only，仅 `knowledge/` | 批量文档上传后并行归类 |
| `kb_explorer` | 快 | read-only，仅 `knowledge/` | 知识检索（Grep/Glob/Read/SemanticSearch） |
| `ops_explorer` | 快 | read-only，仅 CMDB/监控查询工具 | 单一数据源的结构化取证 |
| `investigator` | 中等推理 | read-only，跨数据源只读工具全开 | 根因排查中的一个假设分支（可多个并行） |
| `reviewer` | 高推理 | read-only | 复核 `classifier` 分类冲突 / 复核 `investigator` 结论汇总 |

角色定义遵循 [guide.md 7.6](./guide.md#76-角色目录建议内置--可扩展) 要求：`description` 必须具体到"何时委派"，模糊描述等于不会被委派。

#### 4.2 工具契约

**知识检索类**（对应 [guide.md 4.2](./guide.md#42-推荐工具面)）：

| 工具 | 参数 | 返回 | 副作用分级 |
| :--- | :--- | :--- | :--- |
| `kb_glob` | `pattern, category?` | 文件路径列表 | 读 |
| `kb_grep` | `pattern, category?, mode(files_with_matches\|content), context_lines?` | 匹配片段 + 行号（底层子进程调用 ripgrep，作用域强制限定在 `knowledge/` 目录内，代码层做 realpath 前缀校验防目录穿越） | 读 |
| `kb_read` | `path, offset?, limit?` | 文件内容（单次返回强制截断，大文件分页） | 读 |
| `kb_semantic_search` | `query, category?, top_k` | `[{doc_id, chunk, score}]`（query 先经 Qwen3-Embedding 编码，pgvector 做近似检索，可选再经 reranker 精排） | 读 |

**结构化查询类**：

| 工具 | 参数 | 返回 | 副作用分级 |
| :--- | :--- | :--- | :--- |
| `query_monitor_status` | `target_ids? \| ip_cidr?, since?` | 当前状态（派生自最新事件）+ 最近事件列表 | 读 |
| `query_cmdb` | `asset_ids? \| ip? \| business_system?` | 资产信息（含 owner/位置/所属业务系统） | 读 |
| `query_cmdb_dependencies` | `asset_id, direction(up\|down), max_depth?` | 依赖图遍历结果（`max_depth` 强制上限，防止图过大拖垮上下文） | 读 |

**写操作/提案类**（经 HITL，见第 6 节）：

| 工具 | 参数 | 返回 | 副作用分级 |
| :--- | :--- | :--- | :--- |
| `propose_remediation` | `asset_id, action_type(notify), payload, reason` | 创建 `HitlProposal`（`notify` 可自动批准并执行）；**不直接执行设备命令** | 写（HITL 门控） |
| `query_device_command` | `asset_id, command_name, reason` | 只读诊断命令：白名单+非动态凭据当场执行返回输出；否则 `PENDING` 待审批 | 读（经 HITL 门控） |
| `propose_device_control` | `asset_id, command_name, interface_name?, reason` | 变更类命令（`reboot`/`shutdown`/`port_enable`/`port_disable`）：白名单+非动态凭据当场执行；否则 `PENDING` 待审批 | 写（HITL 门控） |
| `list_device_commands` | `asset_id` | 该资产可用命令名、说明、白/黑名单策略与凭据前提（只读，无审批） | 读 |
| `get_device_query_result` | `proposal_id` | 按会话回查已提交的设备命令查询提案状态或执行结果（只读，无审批） | 读 |

**Spawn 原语类**（照抄 [guide.md 7.2](./guide.md#72-对照三家产品的工具面)）：

```text
spawn_agent(role, task_brief, model?, tools_allowlist?, budget?, fork_mode="none")
wait_agent(child_id, timeout_ms?)
send_input(child_id, message)
close_agent(child_id)          # 幂等；级联关闭子孙
list_agents(session_id)        # 读注册表，不依赖对话记忆
```

所有工具遵循 [guide.md 3.2](./guide.md#32-工具设计原则claude--codex--cursor-共识) 六原则：一工具一契约、副作用分级、结构化 `control` 返回（`ok`/`rejected`/`failed`/`clarification`/`pending_approval`）、大结果截断、错误信息可行动。

### 5. 动态 Spawn 编排

**生命周期状态机**（照抄 [guide.md 7.3](./guide.md#73-生命周期状态机)）：

```text
REQUESTED → SPAWNING → RUNNING → COMPLETED|FAILED|CANCELLED → CLOSED → GC
```

直接映射到 `AgentRegistry.status`。

**关键规则：**

1. **COMPLETED 仍占并发配额，直到显式 `close`**——root loop 每轮编排结束后必须对所有已完成子 Agent 调用 `close_agent`；超时未关闭由后台 GC 任务强制 `detach`（状态改 `CLOSED`，`result_summary` 标注 `force_closed=true`）。
2. **并发上限**：同一 session 内 `max_concurrent_children`（默认 5），用 `asyncio.Semaphore` 控制，对应 [guide.md 9.1](./guide.md#91-多层限额)"并发"层限额。
3. **嵌套深度上限为 2 层**（`root → explorer/worker/classifier/investigator`，再往下最多一层 `reviewer`），运维/知识场景不需要更深的链，比 guide.md 默认的约 3 层更收紧。
4. **`fork_mode` 固定为 `"none"`**（guide.md 7.5 推荐默认）：子 Agent 只收 `task_brief`，不继承父全部历史；父 Agent 必须在 `task_brief` 里显式塞齐必要上下文。
5. **预算划拨**：父 session 有总预算（`max_total_cost_usd`，配置项），`spawn_agent` 时按 [guide.md 9.2](./guide.md#92-子-agent-预算划拨) 划拨 child budget（写入 `AgentRegistry.budget`），子超限只标记该 child `FAILED(reason=budget_exceeded)`，不影响兄弟 child 或父 session。

**两个编排范式（对应 [guide.md 7.7](./guide.md#77-编排模式在动态-spawn-之上)）：**

```mermaid
sequenceDiagram
    participant U as User
    participant Root as Root Agent
    participant C1 as classifier #1..N
    participant Rev as reviewer

    U->>Root: 批量上传 N 份运维文档
    Root->>Root: 判断:批量任务,适合并行
    par 并行归类
        Root->>C1: spawn_agent(classifier, task_brief=doc#1)
        Root->>C1: spawn_agent(classifier, task_brief=doc#2..N)
    end
    Root->>Root: wait_agent(全部)
    alt 分类结果有冲突/新分类提议
        Root->>Rev: spawn_agent(reviewer, task_brief=冲突清单)
        Rev-->>Root: 复核结论
    end
    Root->>Root: close_agent(全部)
    Root-->>U: 归类完成摘要
```

```mermaid
sequenceDiagram
    participant U as User
    participant Root as Root Agent
    participant I1 as investigator(监控历史)
    participant I2 as investigator(CMDB变更)
    participant I3 as investigator(同网段设备)

    U->>Root: 这三个机房怎么同时有设备离线
    Root->>Root: 单一工具查询不够,升级为并行取证
    par 并行假设排查
        Root->>I1: spawn_agent(investigator, task_brief="查历史抖动模式")
        Root->>I2: spawn_agent(investigator, task_brief="查最近变更记录")
        Root->>I3: spawn_agent(investigator, task_brief="查同交换机下其他设备")
    end
    Root->>Root: wait_agent(全部) → 综合摘要
    Root->>Root: close_agent(全部)
    Root-->>U: 根因假设 + 建议(可能带 propose_remediation)
```

**反模式红线**（照抄 [guide.md 7.8](./guide.md#78-反模式)，本项目额外强调）：单个文档分类、单个设备状态查询、告警文案生成——这些是单次动作，禁止 spawn；只有批量并行或多数据源独立取证才允许。

### 6. HITL 状态机

```text
PENDING ──approve──> APPROVED ──resume──> EXECUTED（仅一次）
   └────reject───> REJECTED
```

对应 `HitlProposal.status`，硬规则照抄 [guide.md 5.3](./guide.md#53-hitl-状态机)：

- 只有 `PENDING` 可决定；`APPROVED` 只执行一次（幂等键 = `proposal.id`）
- 待审批期间，`action_payload` 中的敏感字段不通过 WebSocket 回传给发起对话的 Agent 上下文，Agent 只收到"提案已创建，等待审批"的摘要
- 新增权限码 `agent:hitl_approve`，只有持有该权限的用户能操作 `PENDING → APPROVED/REJECTED` 的状态转移（复用现有 RBAC，不新建权限体系）

**执行器分两类**（对应 `action_type`）：

| action_type | 执行器 | 当前状态 |
| :--- | :--- | :--- |
| `notify` | 写 `audit_logs` + 站内消息经 WebSocket 推给相关人 | 已实现，无额外基建需求 |
| `device_control` | Scrapli 执行通道（`send_interactive` / `send_configs`），复用 `device_query` 的命令目录与策略解析 | **已接入**：白名单+静态/无凭据当场执行，动态凭据强制人工审批，命令目录与 `device_query` 共用（见 [docs/superpowers/plans/2026-08-13-device-control-execution.md](./superpowers/plans/2026-08-13-device-control-execution.md)） |

### 7. 确定性管道

**`monitor_sweep`**（asyncio 周期任务，`main.py` lifespan 中启动）：

- 默认每 30 秒一轮，遍历 `MonitorTarget(is_active=true)`
- 探活方式：TCP `asyncio.open_connection(ip, port)` + 超时，不做 ICMP（第一期结论，见第 11 节假设）
- 每轮写一条 `MonitorStatusEvent`（append-only）
- 状态较上一条事件发生翻转（up→down 或 down→up）时，**立即**经 WebSocket 广播给订阅了该资产/网段的活跃 session，不等用户下一次提问

**`cmdb_diff_job`**（低频，默认每小时）：

- 比较"探测到但不在 `CmdbAsset` 里的 IP"（疑似影子资产）与"`CmdbAsset` 登记但从未探测到"（疑似 CMDB 数据过期）
- 发现差异写 `audit_logs`（`action=cmdb_drift_detected`），**不自动增删 CMDB 记录**——发现异常仍是人工核实或走 HITL 提案，不允许确定性任务直接改业务数据

### 8. 会话、压缩与前端实时通道

**Session 最小模型**直接对应 [guide.md 6.1](./guide.md#61-session-最小模型)：`AgentSession` + `AgentMessage` 承担 `messages[]`（完整审计历史，不删除）；`meta` 中的"授权集合/预算/子 Agent 注册表"不塞进 JSON 字段，而是用独立的 `AgentRegistry` 表——这样查询和 GC 更容易，也符合 guide.md"注册表不依赖对话正文"的要求（防止压缩后对话文本里丢了 `child_id`，注册表仍占槽）。

**压缩策略**（P1 阶段先做最简单版本）：最近 N 轮原文 + 早期轮次摘要，触发阈值按 token 计数；根指令（Agent 的角色说明/工具契约）每轮从代码重新注入，不参与压缩，对应 [guide.md 6.3](./guide.md#63-compaction-规范对齐-claude--openai)"System / 根指令永不被摘要吞掉"。

**WebSocket 契约**：单一端点 `/api/v1/ws/agent/{session_id}`，鉴权复用现有 `access_token`（首帧校验）。消息用判别式 JSON：

```text
{"type": "assistant_delta" | "tool_call" | "hitl_pending" | "hitl_resolved" | "monitor_alert" | "error", "payload": {...}}
```

前端一条 WebSocket 连接承载 chat 流式输出、HITL 状态变化、监控告警三类事件，不额外开连接。

**前端页面**：`OpsAssistantPage.tsx`，组件划分沿用现有 `frontend/src/components/{module}/` 惯例：

- `ChatMessageList` / `ChatInput`：流式渲染（参考 shadcn AI Elements 风格组件）
- `HitlApprovalCard`：内嵌在消息流里，`PENDING` 提案渲染成"批准/拒绝"卡片
- `KnowledgeUploadDialog`：权限门控，仅持有 `knowledge:upload` 的用户可见入口

### 9. 安全基线映射（L0–L6）

对应 [guide.md 5.2](./guide.md#52-分层基线l0l6)：

| 层级 | 本项目的具体落地 |
| :--- | :--- |
| L1 能力最小化 | 除 `propose_remediation`、`propose_device_control` 外全部工具只读（`query_device_command` 为只读诊断，经策略门控但不改设备状态）；`kb_grep`/`kb_read` 路径必须落在 `knowledge/` 目录内，代码层做 realpath 前缀校验防目录穿越 |
| L2 动作审查 | `propose_remediation.payload` 做 JSON Schema 校验，`action_type` 白名单枚举，不接受自由文本命令；`propose_device_control` 的 `command_name` 必须在设备命令目录内且通过参数校验（如 `interface_name` 约束），不接受自由文本 CLI |
| L3 风险分级 | `notify` 默认可配置自动批准（低风险）；`device_control` 未分类或需动态凭据时强制 HITL；白名单+非动态凭据凭策略可当场执行，豁免人工审批 |
| L4 执行沙箱 | `device_control` 已接入真实执行通道：白名单+非动态凭据可当场执行；动态凭据强制人工审批；生产启用 `state_changing` 白名单前须在测试网段完成手工验证（见第 11 节 A6） |
| L5 审计 | `AgentMessage`/`MonitorStatusEvent`/`HitlProposal`/`AuditLog` 全部 append-only |
| L6 预算 | 见第 5 节 spawn 预算划拨 + 会话级 `max_total_cost_usd` |

### 10. 可观测性与预算

`AgentTraceEvent` 字段照抄 [guide.md 8.3](./guide.md#83-日志字段建议)：`trace_id, session_id, agent_id, parent_agent_id, step, tool, control, cost_usd, latency_ms, error_class`；错误分类固定四类 `model | tool | policy_reject | infra`。

预算配置分层（对应 [guide.md 9.1](./guide.md#91-多层限额)）：

| 层 | 配置项 | 默认值（可调） |
| :--- | :--- | :--- |
| 单步 | 单次工具输出最大字节数（如 `kb_read` 单次返回上限） | 32KB |
| 单轮 | `max_steps` | 20 |
| 会话 | `max_total_cost_usd` / 累计子 Agent 数 | 视模型定价配置 |
| 并发 | `max_concurrent_children` | 5 |
| 深度 | 最大嵌套层数 | 2 |

### 11. 待明确事项与假设

| # | 假设/待明确 | 说明 |
| :--- | :--- | :--- |
| A1 | 单组织内部部署，不做多租户 | 现有 RBAC 的 `User`/`Role`/`Permission` 模型没有 org 概念，本设计不新增 |
| A2 | Embedding 维度按 Qwen3-Embedding-0.6B 的 1024 维定 | 实测不符，调整 `KnowledgeChunk.embedding` 列定义即可，不影响其他设计 |
| A3 | `ripgrep`（`rg`）作为外部二进制部署到运行环境 | 不是 Python 包依赖，Windows/Linux 都需要单独安装，需写进部署文档 |
| A4 | 监控探活第一期只做 TCP，ICMP 是后续可选扩展点 | 已在工具契约/数据模型里预留空间（`MonitorTarget.port` 必填即代表 TCP 模式），不阻塞后续加 ICMP |
| A5 | WebSocket 断线重连策略留到实现阶段细化 | 本设计只定义消息契约，不定义重连协议 |
| A6 | `device_control` 生产启用前的手工验证 | 已接入 Scrapli 执行通道；**生产启用 `state_changing` 命令白名单前**，须在测试网段真实/虚拟设备上手工验证 `reboot`（`send_interactive` 确认提示命中）、`port_disable`/`port_enable`（`send_configs` 含 Junos `commit`）行为，验证记录归档后方可对生产资产创建白名单策略 |
| A7 | `HitlProposal.action_payload` 中的 `asset_id` 是松引用（存 int 值，不建数据库外键约束到 `CmdbAsset`） | 使 T08（CMDB）和 T10（HITL）可以并行独立开发；校验 `asset_id` 是否存在留给调用 `propose_remediation` 的工具实现层（届时 T08 应已就绪），不在数据库层强耦合 |

---

## Part B: 任务分解

### 12. 新增依赖

**后端（`uv add`）：**

```bash
cd backend
uv add pgvector          # SQLAlchemy 的 pgvector 类型支持
uv add httpx              # 调用本地 llama.cpp OpenAI 兼容接口（现有 httpx 仅在 dev 依赖，需提升为生产依赖）
```

**外部二进制（非 Python 包，需单独安装到运行环境）：**

| 依赖 | 用途 |
| :--- | :--- |
| `ripgrep`（`rg`） | `kb_grep` 工具的底层实现 |
| PostgreSQL `pgvector` 扩展 | 通过 Alembic migration 执行 `CREATE EXTENSION IF NOT EXISTS vector` |

**前端：** 暂不确定具体包名，实现阶段核实 shadcn AI Elements 相关组件后再定。

### 13. 任务列表（延续 T01–T05 编号）

> 沿用 [ARCHITECTURE.md](./ARCHITECTURE.md) 的任务编号习惯，从 T06 开始；每个任务对应 [guide.md 12.1](./guide.md#121-渐进交付) 的分期。

| 任务 | 内容 | 对应 guide.md 分期 | 依赖 |
| :--- | :--- | :--- | :--- |
| **T06** | Agent 内核基建：`app/agent/loop.py`、`session.py`、`budget.py`，`app/core/llm.py`（MODELS 登记表），数据模型 `AgentSession`/`AgentMessage`/`AgentRegistry`/`HitlProposal`/`AgentTraceEvent`，对应 Alembic 迁移 | P0–P1 | 无（新地基） |
| **T07** | 知识库子系统：`KnowledgeCategory`/`Document`/`Chunk` 模型，`kb_glob`/`kb_grep`/`kb_read`/`kb_semantic_search` 工具，上传 API，pgvector 集成，`classifier`/`kb_explorer` 角色 | P2 | T06 |
| **T08** | CMDB + 监控子系统：`CmdbAsset`/`CmdbAssetDependency`/`MonitorTarget`/`MonitorStatusEvent` 模型，`monitor_sweep`/`cmdb_diff_job` 确定性任务，`query_cmdb`/`query_monitor_status`/`query_cmdb_dependencies` 工具，`ops_explorer` 角色 | 不依赖 spawn，纯确定性管道 + 只读工具 | T06 |
| **T09** | Spawn 编排 + 角色目录：`app/agent/spawn.py`，`ChildReceipt` 落地，`investigator`/`reviewer` 角色，两个编排范式（批量归类并行、根因排查并行） | P3–P4 | T07 + T08 |
| **T10** | HITL + 安全闸门：`app/agent/hitl.py` 状态机，`propose_remediation`/`propose_device_control`/`query_device_command` 工具，设备命令经 Scrapli `DeviceQueryExecutor` 执行通道落地，新增权限码（`knowledge:*`/`cmdb:*`/`monitor:*`/`agent:hitl_approve`） | P1 + 安全基线（第 9 节） | T06 |
| **T11** | 前端 Chat 页面：`OpsAssistantPage`、WebSocket 客户端、消息流组件、`HitlApprovalCard`、`KnowledgeUploadDialog` | 对应前端集成 | T06（至少要有可用的 WS 端点，可用 mock 提前并行开发） |

### 14. 任务依赖图

```mermaid
graph TD
    T06[T06: Agent 内核基建<br/>loop+session+llm+核心数据模型]
    T07[T07: 知识库子系统<br/>文档+检索+classifier/kb_explorer]
    T08[T08: CMDB+监控子系统<br/>确定性管道+只读查询工具]
    T09[T09: Spawn 编排+角色目录<br/>investigator/reviewer+两个编排范式]
    T10[T10: HITL+安全闸门<br/>状态机+权限码]
    T11[T11: 前端 Chat 页面]

    T06 --> T07
    T06 --> T08
    T06 --> T10
    T06 --> T11
    T07 --> T09
    T08 --> T09

    style T06 fill:#4ade80,stroke:#16a34a,color:#000
    style T07 fill:#60a5fa,stroke:#2563eb,color:#000
    style T08 fill:#60a5fa,stroke:#2563eb,color:#000
    style T09 fill:#f59e0b,stroke:#d97706,color:#000
    style T10 fill:#60a5fa,stroke:#2563eb,color:#000
    style T11 fill:#f59e0b,stroke:#d97706,color:#000
```

**并行说明：**

- T06 完成后，T07 / T08 / T10 / T11 可并行开发（各自独立子系统）
- T09（spawn 编排）依赖 T07 + T08 同时就绪——因为两个编排范式（批量归类、根因排查）分别需要知识库工具和运维查询工具
- T11 前端可以在 T06 提供可用 WS 端点后，用 mock 数据提前并行开发，不必等 T07/T08/T09 全部完成

---

> **文档结束**
> 本文档不修改 [docs/guide.md](./guide.md)，技术路线均以其为参照来源；与 [docs/ARCHITECTURE.md](./ARCHITECTURE.md)（RBAC 基座）互为补充，不重复其内容。
