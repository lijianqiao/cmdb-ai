# Agent 开发范式手册（2026-08）

> **定位**：跨项目可复用的 Agent 工程规范。照着做下一个 Agent，不必依赖任何既有业务仓库。  
> **对齐对象**：OpenAI Codex Subagents、OpenAI Agents SDK、Anthropic Claude Code / Agent SDK、Cursor Subagents（截至 2026-08 公开文档与实践）。  
> **核心取向**：**动态 spawn 多 Agent** + **混合知识检索（Grep 优先，向量补充）** + **代码强制安全与预算**。

---

## 目录

1. [Agent 是什么](#1-agent-是什么)
2. [标准 Agent 循环](#2-标准-agent-循环)
3. [工具与确定性核心](#3-工具与确定性核心)
4. [混合知识库](#4-混合知识库grep-优先向量补充)
5. [安全基线与沙箱](#5-安全基线与沙箱)
6. [多轮会话与上下文压缩](#6-多轮会话与上下文压缩)
7. [动态 Spawn 多 Agent](#7-动态-spawn-多-agent主轴)
8. [可观测性](#8-可观测性)
9. [成本与配额](#9-成本与配额)
10. [契约与版本](#10-契约与版本)
11. [Eval](#11-eval)
12. [交付路线图与脚手架](#12-交付路线图与脚手架)
13. [参考来源](#13-参考来源)

---

## 1. Agent 是什么

### 1.1 定义（OpenAI）

Agent 是**代表用户、以较高自主度完成工作流**的系统。它具备：

- 用 LLM **管理工作流执行**（决定下一步做什么）
- 能识别完成 / 失败，并把控制交回用户
- 在明确护栏内，动态选择工具与外部环境交互

**不是 Agent**：单次 Chat、固定流水线、只靠 system prompt「请别干坏事」却无代码闸门。

### 1.2 三大积木

| 积木 | 含义 |
| :--- | :--- |
| **Model** | 推理与决策；可按角色换档位（快扫 / 深思） |
| **Tools** | 对外动作；契约清晰、副作用可分级 |
| **Instructions** | 行为习惯与工作约定（可被压缩；**硬安全不放这里**） |

### 1.3 何时该建 Agent

优先做 Agent 的场景：判断含糊、规则难穷尽、依赖非结构化输入、路径不固定。  
规则清晰、必须 100% 同路径复现、延迟极紧 → 用确定性管道，不要硬上 Agent。

### 1.4 2026 共识取向

| 主题 | 共识 |
| :--- | :--- |
| 编排 | 外壳代码保证安全与退出；内核让模型在允许工具集内自主探索 |
| 检索 | 标识符 / 结构化知识用 Grep；语义补充用向量；检索是**工具**不是预塞 prompt |
| 多 Agent | 默认动态 spawn；独立上下文；回传摘要；显式 close 释放配额 |
| 安全 | 判断动作结构，不看措辞；沙箱 + 权限 + HITL 纵深 |
| 质量 | 代码内可移植 Eval；规则优先，LLM judge 兜底 |

---

## 2. 标准 Agent 循环

### 2.1 循环本体（行业同构）

OpenAI Agents SDK `Runner`、Claude Agent SDK、Codex/Cursor harness 本质相同：

```text
messages = [system] + history + [user]
for step in 1 .. max_steps:
    if budget_or_token_exceeded:
        return BUDGET_EXCEEDED
    response = llm.chat(messages, tools=TOOL_DEFS)
    append assistant message (含 tool_calls)
    if no tool_calls:
        return FINAL_ANSWER                    # 退出 A
    for each tool_call:
        out = dispatch(tool_call)              # 闸门在代码里
        if out.control in {clarify, hitl, handoff_stop}:
            return EARLY_EXIT(out)             # 退出 B
        append tool result
return FAILED_MAX_STEPS                        # 退出 C
```

**不变量：**

1. 模型只决定「问什么 / 调什么」；「能不能做」由工具内代码决定。  
2. 有界：`max_steps` / `max_turns` + token 上限 + 成本预算。  
3. 工具结果回灌同一条 messages，拒绝原因必须可行动（让循环能自愈）。  
4. Plan / Todo 只做观测，不自动当执行引擎。

### 2.2 官方循环语义（OpenAI Agents SDK）

1. 调用当前 Agent  
2. 产出 final output → 结束  
3. handoff → 换 Agent 再跑  
4. 否则执行 tool_calls，回到 1  
5. 超过 `max_turns` → 失败（可配置）

### 2.3 控制信号（建议统一）

工具返回结构化 `control`，不要靠解析自然语言：

| control | 含义 |
| :--- | :--- |
| `ok` | 继续循环 |
| `rejected` | 策略拒绝，回灌原因后让模型改写 |
| `failed` | 基础设施/校验失败 |
| `clarification` | 需要用户澄清，结束本轮 |
| `pending_approval` | HITL，结束本轮并挂起 |
| `spawned` / `child_done` | 多 Agent 生命周期事件（见第 7 节） |

---

## 3. 工具与确定性核心

### 3.1 确定性核心 vs 模型边缘

把**不能错、要可审计**的逻辑写死在代码里；模型只做选择与解释。

| 放代码（确定性核心） | 放模型（边缘） |
| :--- | :--- |
| 权限、AST/结构审查、沙箱策略 | 选哪个工具、怎么拆任务 |
| 状态机（HITL、子 Agent 生命周期） | 写查询 / 改代码的内容 |
| 指标公式、账本/库存校验 | 对结果的自然语言解释 |
| 配额扣减、幂等键、审计落盘 | 何时澄清、何时 spawn |
| 「最终用了什么资源」的派生标注 | 面向用户的叙述 |

**原则**：不信任模型自报（「我用了哪些表 / 我已脱敏 / 子 Agent 已关闭」）。凡是审计字段，从执行结果或注册表**代码派生**。

### 3.2 工具设计原则（Claude / Codex / Cursor 共识）

1. **少而硬**：一个工具一个契约；禁止万能 `execute_anything`。  
2. **副作用分级**：读 / 写 / 花钱 / 外网 —— 默认拒绝高风险，显式放开。  
3. **结果结构化**：成功体 + `control` + 可行动错误。  
4. **大结果截断**：避免单次工具输出撑爆上下文（探索类工作优先交给子 Agent）。  
5. **错误可行动**：说明「为什么失败 + 怎么改」。  
6. **Skills vs Subagents**：单次、可复用的短动作用 Skill/Command；需要独立上下文、多步、并行时用 Subagent（Cursor 官方区分）。

### 3.3 工具协议（OpenAI 兼容）

```text
assistant → content? + tool_calls[]
tool      → tool_call_id + content
```

实现要点：assistant 消息必须原样保留 `tool_calls`；未知工具 / 坏 JSON / schema 失败 → 结构化 error 回灌，不崩循环。

---

## 4. 混合知识库：Grep 优先，向量补充

### 4.1 为什么不是「纯 RAG」也不是「纯 Grep」

| 方式 | 强项 | 弱项 |
| :--- | :--- | :--- |
| **Grep / Glob / Read**（Claude Code、Cursor 主力） | 标识符精确、永远读当前态、无索引漂移、隐私好 | 弱关键词、跨语言语义、长文模糊查询弱 |
| **向量召回** | 语义相近、表述不一致时仍能捞到 | 噪声、索引滞后、运维成本、标识符易漂 |

**推荐默认策略（Cursor 实践 + Claude 取向）：**

```text
1. Glob / 目录地图缩小范围
2. Grep 精确标识符、错误码、表名、API 名
3. Read 命中文件的相关段落
4. 若仍不足 → 向量补充召回（semantic search）
5. 模型再决定是否继续 Grep / Read
```

检索必须是 **Agent 可调用的工具**，而不是服务端一次性 top-k 塞进 system prompt。

### 4.2 推荐工具面

| 工具 | 作用 |
| :--- | :--- |
| `Glob` | 路径模式找文件 |
| `Grep` | 正则内容搜索（建议 ripgrep）；支持 files_with_matches / content / 行号 |
| `Read` | 全文或行区间；大文件强制分页 |
| `SemanticSearch`（可选） | 向量补充；返回路径+片段，再交给 Read 精读 |
| `Bash`（可选、沙箱内） | `jq` / `git log` 等长尾；默认收紧 |

### 4.3 知识落盘纪律

```text
knowledge/
  INDEX.md              # 主题 → 路径地图
  policies/
  schemas/
  playbooks/
  examples/
```

写作要求：

1. 一主题一文件；文件名与稳定术语一致。  
2. 标题/锚点用可 Grep 的原文标识符。  
3. `INDEX.md` 只做地图。  
4. 变更 = 改文件；Grep 立即生效；向量索引异步重建（允许短暂滞后）。

### 4.4 授权集合（重要）

「读过哪些知识 / 允许碰哪些资源」由**代码维护的授权集合**记录，不由模型嘴上声明。  
后续高风险工具（如查询、写库）必须校验授权集合。

### 4.5 何时加重向量权重

- 海量非结构化长文、OCR、客服记录  
- 跨语言语义查询为主  
- 命名极不规范、Grep 命中率长期偏低  

即便如此，仍建议：**Grep 打标识符 → 向量补召回 → Read 精读**，而不是只用向量。

---

## 5. 安全基线与沙箱

### 5.1 第一原则

> **安全判断只看要执行的动作，不看用户怎么问。**

同一动作，客气问与挑衅问必须同一结果。用配对 Eval 锁住这条不变量。

### 5.2 分层基线（L0–L6）

```text
L0 威胁模型：假设 prompt injection / 工具结果投毒会发生
L1 能力最小化：默认无写、无任意 shell、无裸外网
L2 动作审查：AST / 结构化校验（不是关键字黑名单）
L3 风险分级：合法但敏感 → HITL
L4 执行沙箱：审查漏了也执行不了危险副作用
L5 审计：状态变迁只追加；可回放
L6 预算：步数 / token / 美元 / 子 Agent 并发与深度
```

### 5.3 HITL 状态机

```text
PENDING ──approve──> APPROVED ──claim──> EXECUTING ──success──> EXECUTED
   └────reject───> REJECTED                      └─failure/crash──> UNKNOWN
UNKNOWN ──confirm_executed──> EXECUTED（人工确认）
UNKNOWN ──allow_retry──────> APPROVED（检查后允许重试）
```

硬规则：只有 `PENDING` 可审批决定；`REJECTED` / `EXECUTED` 为终态；认领 `EXECUTING` 前须策略复检且事务先提交，外部执行器才启动；`UNKNOWN` 不自动重试（人工处置见 §5.3.1）；待审批时敏感结果**不回传模型**；落盘原子写。

#### 5.3.1 管理员处置 UNKNOWN 提案（本项目）

当 HITL 提案进入 `UNKNOWN`（执行中断、外部通道超时或进程崩溃后启动恢复），系统**不会自动重试**。持有 `agent:hitl_approve` 权限的管理员须在运维助手消息流中的 `HitlApprovalCard` 或 API 人工处置：

1. **先核实真实设备/通知状态**（必做）：登录目标设备或查阅监控系统，确认该提案对应的操作（重启、端口变更、通知发送等）在物理世界是否已生效。**在未完成此检查前，禁止点击「确认已执行」。**
2. **若操作已在设备上生效**：调用 `POST /api/v1/hitl/proposals/{id}/resolve-unknown`，body `{"resolution": "confirm_executed"}`（或前端「确认已执行」）。系统将提案标记为 `EXECUTED` 并写入 `executed_at`，仅做状态对齐，不会再次下发命令。
3. **若操作未生效且策略仍允许执行**：调用同一接口，body `{"resolution": "allow_retry"}`（或前端「允许重试」）。提案回到 `APPROVED`；管理员或 Agent 再调用 `POST /api/v1/hitl/proposals/{id}/retry` 触发重新执行（动态凭据资产须附带本次密码）。
4. **若策略已变更或不应再执行**：保持 `UNKNOWN` 或走业务流程关闭会话；不要误点「确认已执行」。

`UNKNOWN` 状态下 Agent 循环收到 `HitlResumeError`，不会静默重试；用户侧应看到明确的中文状态说明。

### 5.4 沙箱选型（Anthropic）

| 方案 | 隔离强度 | 适用 |
| :--- | :--- | :--- |
| 权限门（allow / deny / ask） | 低 | 交互式开发 |
| 内置 Bash 沙箱（FS + 网络） | 中 | 日常命令 |
| sandbox-runtime | 中+ | 无 Docker 的 OS 级限制 |
| Dev Container / Docker | 高 | 团队标准 / CI |
| gVisor / 独立 VM | 很高 | 多租户、不可信内容 |

**两层独立策略**：文件系统白名单 × 网络域名白名单。高安全场景用 TLS 终结代理，防 domain fronting。

部署铁律（Claude Secure Deployment）：

- Agent 跑在隔离边界**之内**  
- 密钥在边界**之外**，经代理注入  
- 非 root；不挂载宿主机敏感目录  
- 跳过权限确认的模式必须套容器/VM  
- 仓库内说明文件可被污染；**强制策略放托管配置 / hooks / 代理**

### 5.5 子 Agent 的安全继承（Codex / Claude）

- 子 Agent **继承父会话沙箱与审批策略**（Codex：含交互期 `/permissions` 覆盖）  
- 可为角色覆盖更严策略（如 explorer = `read-only`）  
- 背景子 Agent：预先批准工具集；未批准的自动拒绝  
- Claude：可对子 Agent 输出做扫描；**不能替代**工具权限与沙箱  
- 写操作并行要克制（Codex：并行适合读多写少；多人同写易冲突）

---

## 6. 多轮会话与上下文压缩

### 6.1 Session 最小模型

```text
Session {
  id
  messages[]              # 完整审计历史（不要删）
  meta                    # 授权集合、预算、业务状态、子 Agent 注册表
  memory_summary?         # 压缩后的工作摘要
}
```

每轮：`build_model_history`（有界）→ `run_turn` → append → 必要时 compact → save。

### 6.2 为什么需要压缩

Codex 术语：

- **Context pollution**：噪声中间输出淹没决策信息  
- **Context rot**：窗口变脏后可靠性下降  

解法：噪声工作丢给子 Agent；主会话只留决策与摘要；再对主会话做 compaction。

### 6.3 Compaction 规范（对齐 Claude / OpenAI）

| 来源 | 做法 |
| :--- | :--- |
| Claude Code | 接近窗口上限自动压缩；`/compact` 可手动；**根指令文件与 auto-memory 从磁盘重注入**，不被摘要吃掉 |
| OpenAI Agents SDK | `responses.compact` / `OpenAIResponsesCompactionSession`；阈值触发或阶段边界强制 compact |
| 通用建议 | System / 根指令永不被摘要吞掉；摘要进对话流或专用 compaction 块 |

**必须保留、不能只靠对话文本的状态**（放 `meta`）：

- 活跃 / 已完成子 Agent ID 与回执  
- HITL 提案 ID  
- 授权集合、幂等键、累计费用  

压缩后跑 `reconcile_children()`：防止「对话里丢了 child_id，注册表仍占槽」。

### 6.4 记忆分层

| 层 | 内容 | 存活 |
| :--- | :--- | :--- |
| 根指令（AGENTS.md / CLAUDE.md 类） | 项目硬约定 | 每轮从磁盘加载；压缩后重注入 |
| Auto-memory / 主题笔记 | 可 grep 的短笔记 | 有界；按需 Read |
| 会话摘要 | 早期轮次压缩结果 | 随 compact 更新 |
| 最近窗口 | 最近 N 轮原文 | 原样保留 |
| 完整 transcript | 全量消息 | 落盘审计；不整包塞回模型 |

续问友好：写入历史时附带「本轮技术要点」（关键标识符、最终动作摘要），用户可见文本保持干净。

---

## 7. 动态 Spawn 多 Agent（主轴）

> 本节是多 Agent 的**默认设计**。静态 handoff / agents-as-tools 可作为补充，但不替代动态生命周期。

### 7.1 为什么动态 spawn（Codex / Claude / Cursor 共识）

- 主线程专注需求、决策、最终输出  
- 探索 / 日志 / 浏览器等噪声在子上下文消化，只回传摘要  
- 可并行；可用更快更便宜的模型跑探索  
- 角色可窄：explorer / worker / reviewer 各一套工具与沙箱  

### 7.2 对照三家产品的工具面

| 能力 | Codex | Claude Agent SDK | Cursor |
| :--- | :--- | :--- | :--- |
| 创建 | `spawn_agent` | `Agent` 工具 + AgentDefinition | `Task` 工具 |
| 等待 | `wait` | 前台阻塞或后台 + 通知 | foreground / background |
| 输入 | `send_input` / resume | 跟进消息 | resume / 跟进 |
| 销毁 | `close_agent`（完成仍占槽直到 close） | 会话保留 transcript；清理周期 | 完成后回收；后台可中断 |
| 角色定义 | `.codex/agents/*.toml` | 代码 `agents{}` 或 `.claude/agents/` | `.cursor/agents/*.md` |
| 内置角色例 | default / worker / explorer | general-purpose 等 | Explore / Bash / Browser |

**实现你自己的系统时，统一暴露这组原语：**

```text
spawn_agent(task_name, message, agent_type?, model?, reasoning?, fork_mode?)
wait_agent(target, timeout_ms?)
send_input(target, message)
close_agent(target)          # 幂等；级联关闭子孙
list_agents() / get_status() # 读注册表，不依赖对话记忆
```

### 7.3 生命周期状态机

```text
REQUESTED → SPAWNING → RUNNING → COMPLETED|FAILED|CANCELLED → CLOSED → GC
```

要点（来自 Codex 公开行为与踩坑）：

1. **COMPLETED 仍可能占并发配额，直到 close** —— 设计里要强制「用完即关」。  
2. `close` **幂等 + 超时强制 detach**，防止子线程挂死导致槽位永久泄漏。  
3. 注册表在 **Session.meta / 独立 store**，不依赖对话正文（压缩会丢掉 ID）。  
4. 父会话结束 → 级联 shutdown 整棵子树。

### 7.4 ChildReceipt（每次 spawn 必有回执）

```text
ChildReceipt {
  child_id
  parent_id
  agent_path          # 如 /root/task1/review_security
  role                # explorer | worker | reviewer | ...
  model
  reasoning_effort?
  tools_allowlist
  sandbox_mode
  task_brief
  budget { max_steps, max_cost_usd, max_wall_time }
  status
  result_summary      # 回传父上下文的唯一正文
  artifacts[]         # 文件路径、diff、日志指针
  created_at / closed_at
}
```

没有回执 = 没有可审计的多 Agent。

### 7.5 上下文继承策略（Codex V2 思路）

| fork_mode | 行为 | 适用 |
| :--- | :--- | :--- |
| `none`（推荐默认） | 干净上下文；只收 task brief | 探索、评审、并行工人 |
| `all` / fork | 继承父历史；可省缓存成本 | 需要同一决策上下文的短分叉 |

规则：

- 覆盖 `agent_type` / `model` / `reasoning` 时，通常应 `fork_mode=none`（避免「全量历史 + 另一套人格」纠缠）。  
- 子 Agent **默认不看到**父的全部工具噪声；父在 prompt 里塞齐必要上下文。  
- Claude：子 transcript 独立存储；主会话 compact **不影响**子 transcript。  
- 嵌套深度设上限（Claude 默认约 3 层可配）；图保持近似 DAG，禁止互踢死循环。

### 7.6 角色目录（建议内置 + 可扩展）

| 角色 | 模型倾向 | 沙箱 | 典型工作 |
| :--- | :--- | :--- | :--- |
| `explorer` | 快、便宜 | read-only | Grep/Glob/Read 摸清结构 |
| `worker` | 中等 | workspace-write | 实现与小修复 |
| `reviewer` | 高推理 | read-only | 正确性 / 安全 / 测试缺口 |
| `docs_researcher` | 快 | read-only + docs MCP | 核对外部 API 文档 |
| `browser` | 中高 | 受限网络 | 复现 UI、收集证据 |

自定义角色：`name` + `description`（决定何时委派）+ `instructions` + 可选 `model` / `sandbox` / `tools`。  
**description 要写具体**（Cursor / Claude 都强调）；模糊 description 等于不会被委派。

### 7.7 编排模式（在动态 spawn 之上）

```text
主 Agent（面向用户）
  ├─ spawn explorer × N（并行只读）
  ├─ wait_all → 综合摘要
  ├─ spawn worker（串行写入，避免冲突）
  ├─ spawn reviewer（只读验收）
  └─ close 全部 → 最终回答
```

补充（OpenAI SDK，可选）：

- **Agents as tools**：专家不抢用户话权，经理综合结果  
- **Handoffs**：专家接管对用户说话  

动态 spawn 与二者可组合，但**生命周期与配额仍以注册表为准**。

### 7.8 反模式

- 把全量父历史默认 fork 给每个子 Agent  
- 完成不 close，靠压缩「忘掉」子 Agent  
- 用对话文本当唯一注册表  
- 多个 worker 并行改同一文件无协调  
- 为「生成 changelog」这种单次动作建子 Agent（应用 Skill）  
- 子 Agent 工具集过宽（explorer 不应能写生产）  

---

## 8. 可观测性

### 8.1 目标

任意一次失败都能回答：卡在哪一步、调了什么工具、花了多少钱、哪个子 Agent、是否被策略拒绝。

### 8.2 最小埋点（对齐 OpenAI Tracing）

| Span | 内容 |
| :--- | :--- |
| `trace` | 一次用户任务 |
| `agent` | 某个 Agent 实例（含 child_id） |
| `generation` | 一次模型调用（模型、tokens、费用） |
| `tool` | 工具名、参数摘要、control、耗时 |
| `guardrail` | 审查/HITL 结果 |
| `spawn` / `close` | 子 Agent 生命周期 |

导出：OpenTelemetry 或等价；开发期可看瀑布图，生产期可报警。

### 8.3 日志字段建议

`trace_id`, `session_id`, `agent_id`, `parent_agent_id`, `step`, `tool`, `control`, `cost_usd`, `latency_ms`, `error_class`

错误分类：`model` | `tool` | `policy_reject` | `infra` —— 否则 Eval「失败」无法归因。

---

## 9. 成本与配额

### 9.1 多层限额

| 层 | 限制什么 |
| :--- | :--- |
| 单步 | 单次工具输出大小、单次模型 max tokens |
| 单轮 | `max_steps`、单轮美元/token |
| 会话 | 累计美元、累计子 Agent 数 |
| 并发 | `max_concurrent_threads_per_session`（Codex 同名概念） |
| 深度 | 最大 spawn 嵌套层数 |
| 日/租户 | 账户级熔断 |

### 9.2 子 Agent 预算划拨

父预算 → 创建时划拨 child budget → 子超限只杀子，尽量不直接烧穿父。  
并行探索用小模型；深思评审再用高推理档（Codex：快扫 vs high effort 分工）。

### 9.3 成本现实

子 Agent 工作流**通常比单 Agent 更费 token**（Codex 明确说明）。用隔离换的是主上下文质量与并行墙钟时间，不是绝对更便宜。要用 Eval 看「$/成功任务」。

---

## 10. 契约与版本

### 10.1 需要版本化的契约

| 契约 | 为什么 |
| :--- | :--- |
| 工具 JSON Schema | 参数变更会静默破坏调用 |
| System / 角色 instructions | 行为漂移不可比 |
| Judge rubric / prompt | Eval 分数不可横比 |
| 知识文件重大变更 | 检索行为变化 |
| ChildReceipt / Session meta schema | 恢复与审计依赖 |

改了就升版本号；旧分数、旧 trace 标注旧版本。

### 10.2 幂等与副作用

- 写操作带 idempotency key  
- HITL resume、close_agent、支付类动作只生效一次  
- LLM 调用可重试；已提交副作用不可盲目重试  

### 10.3 配置分层

| 层 | 例子 | 谁能改 |
| :--- | :--- | :--- |
| 托管强制策略 | 沙箱、拒绝规则、MCP 白名单 | 管理员 |
| 项目约定 | AGENTS.md / 角色描述 | 开发者 |
| 运行时覆盖 | 本轮权限模式 | 用户交互 |

强制策略不得仅存在于可被仓库内容改写的文件里。

---

## 11. Eval

### 11.1 2026 取向

平台托管 Eval 会变迁/关停；**代码内、可移植的 Eval 套件**才是长期方案。  
评分顺序：**结果 → 轨迹不变量 → 效率 → 语义 judge → 人工校准**。

### 11.2 分层怎么判

| 层 | 问题 | 方法 |
| :--- | :--- | :--- |
| Outcome | 最终状态对不对 | 确定性检查 |
| Trajectory invariants | 该发生/禁止发生的事件 | 必调工具、禁止写、HITL 前不得见数 |
| Efficiency | 是否浪费 | steps、重复工具、$、延迟分位 |
| Semantic | 表述是否合格 | 异源 LLM judge + 固定 rubric |
| Human | Judge 是否靠谱 | 抽样复核台账 |

**不要**要求唯一正确工具序列；用 invariants。  
Judge 与被测模型必须不同源；解析失败 = FAIL，绝不默默 PASS。  
Rubric / judge prompt 版本化。

### 11.3 建议用例类别

- 主路径成功  
- 歧义澄清  
- 策略拒绝（含措辞配对）  
- HITL  
- 多轮指代续问（压缩后）  
- 并行 spawn + close（无泄漏）  
- 预算熔断  
- Grep 命中 vs 向量补充路径  

### 11.4 Agent 指标

任务成功率、危险动作零通过、HITL 召回/误报、多工具率、P50/P95 延迟、$/task、子 Agent 槽位泄漏率、压缩后续问正确率。

---

## 12. 交付路线图与脚手架

### 12.1 渐进交付

```text
P0  单 Agent 循环 + 只读工具 + L1–L4 安全 + 10 条 Eval
P1  多轮 Session + compaction + HITL + 观测/预算
P2  混合知识库（Grep+向量）+ 授权集合
P3  动态 spawn：explorer 并行 + wait + close + 注册表
P4  worker/reviewer 角色 + 沙箱分档 + 预算划拨
P5  嵌套深度、级联销毁、compact reconcile、产品化面板
```

### 12.2 脚手架勾选

**循环**

- [ ] `max_steps` + 预算 + 结构化 control  
- [ ] 统一模型调用入口  

**工具**

- [ ] Schema 校验；无万能执行器；大结果截断  

**知识**

- [ ] Glob / Grep / Read  
- [ ] SemanticSearch 补充  
- [ ] 授权集合由代码维护  

**安全**

- [ ] 动作级审查 + 执行沙箱 + HITL + 措辞配对 Eval  

**会话**

- [ ] 完整历史落盘；有界 `build_model_history`；meta 存硬状态  

**多 Agent**

- [ ] `spawn` / `wait` / `send_input` / `close`  
- [ ] ChildReceipt + 注册表  
- [ ] 完成必 close；close 超时强制释放  
- [ ] compact 后 reconcile  

**观测与成本**

- [ ] trace/span；$/task；并发与深度上限  

**契约与 Eval**

- [ ] Schema/prompt 版本号；规则优先；异源 judge  

---

## 13. 参考来源（2026-08）

- OpenAI, *A practical guide to building agents*  
- OpenAI Agents SDK: Running agents / Sessions / Compaction / Tracing / Multi-agent / Guardrails  
- OpenAI Codex: [Subagents 概念与配置](https://developers.openai.com/codex/concepts/subagents)（spawn、并发上限、角色 toml、沙箱继承）  
- Anthropic: [Claude Code Subagents](https://code.claude.com/docs/en/sub-agents)、[Agent SDK Subagents](https://code.claude.com/docs/en/agent-sdk/subagents)、[Secure deployment](https://code.claude.com/docs/en/agent-sdk/secure-deployment)、[Sandboxing](https://code.claude.com/docs/en/sandboxing)  
- Cursor: [Subagents](https://cursor.com/docs/subagents)、[Agent best practices](https://cursor.com/blog/agent-best-practices)（Grep + semantic search、Task 委派、Explore 隔离噪声）  
- OpenAI Cookbook: *Building Reliable Agents with Memory and Compaction*  
- Eval: OpenAI Agent evals / Evaluation best practices；轨迹不变量优于唯一路径编排  

---

*文档版本：2026-08-10 · 与具体业务仓库解耦 · 以动态 spawn 为多 Agent 主轴 · 知识检索默认混合策略。*
