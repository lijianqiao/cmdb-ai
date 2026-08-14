# Agent 运行时可靠性修复设计

**状态**：已与项目所有者确认设计（2026-08-14）。

## 1. 目标

一次纳入本轮前后端审查发现的全部问题，按风险分四个阶段修复：

1. 设备变更执行安全与根会话串行化。
2. 上下文压缩、会话快照与前端断线恢复。
3. 将现有 SpawnManager 接入真实运维助手。
4. WebSocket 背压、前端分包、静态检查和文档收尾。

完成后，设备命令不会因旧策略、并发审批或进程崩溃被静默重复执行；聊天、HITL 和子 Agent 状态可以从数据库恢复；根 Agent 可以自动编排只读子 Agent；全部自动化测试和静态检查通过。

## 2. 全局约束

- 只在 `master` 分支工作，不创建或切换分支，不创建 PR。
- 每个阶段遵循测试先行：先写失败测试，再做最小实现。
- Python 固定使用 3.14.3，所有 Python 命令使用 `uv run`。
- 不新增依赖；若实施时确实需要新增依赖，必须先说明原因并使用 `uv add` 安装最新兼容版本。
- 测试使用假 LLM、假设备执行器和假通知器，不访问真实设备，不产生模型费用。
- 继续采用单 Uvicorn worker；不引入 Redis、Celery 或分布式任务运行时。
- 设备凭据只在现有安全边界内使用，不进入消息、WebSocket、子 Agent task brief、状态原因或审计详情。

## 3. 总体架构

修复采用“按风险分阶段、前后端垂直打通”的方式。每个阶段产生一组可独立验收和提交的能力，必须通过该阶段测试门禁后才能进入下一阶段。

核心边界如下：

- 数据库是 HITL、根会话、消息和子 Agent 生命周期的恢复依据。
- WebSocket 只负责低延迟通知，不是状态真相来源。
- 外部设备或通知操作不与聊天 transcript 共用事务。
- 根 Agent 可以自动创建只读子 Agent，但所有变更动作仍只允许根 Agent经过 HITL 发起。
- 审计消息永久保留；压缩和分页只改变模型窗口与前端读取窗口。

## 4. 阶段一：HITL 执行安全

### 4.1 状态机

HITL 状态机扩展为：

```text
PENDING --批准--> APPROVED --原子认领并提交--> EXECUTING
   |                                             |
   +--拒绝--------------------------------> REJECTED
                                                 |
                          明确成功 --------------> EXECUTED
                          结果不能确定 ----------> UNKNOWN

UNKNOWN --管理员确认已执行----------------------> EXECUTED
UNKNOWN --管理员检查后允许重试------------------> APPROVED
```

规则：

- `PENDING` 只能进入 `APPROVED` 或 `REJECTED`。
- `APPROVED` 只有成功通过执行前检查后才能进入 `EXECUTING`。
- `EXECUTING` 不能直接重试。
- 外部调用开始后发生的异常、超时、连接中断或进程崩溃统一视为结果不确定，进入 `UNKNOWN`。
- `UNKNOWN` 禁止自动重试，只能由持有 `agent:hitl_approve` 的管理员人工处置。
- 管理员确认设备实际已执行时进入 `EXECUTED`；管理员检查设备后明确允许重试时回到 `APPROVED`。
- 服务启动时将遗留 `EXECUTING` 提案恢复为 `UNKNOWN`。

`hitl_proposals` 增加：

- `execution_started_at`：本次执行被持久化认领的时间。
- `status_reason`：安全、有限长度的状态原因代码，不保存异常原文或凭据。
- `resolved_by_user_id`：人工处置 `UNKNOWN` 的管理员。
- `resolved_at`：人工处置时间。

现有 `executed_at` 继续表示已执行时间；管理员确认已执行时写入确认时间，并通过 `status_reason` 区分“执行器明确成功”和“人工确认成功”。

### 4.2 执行前策略复检

每一次执行尝试都必须重新读取：

- 提案及其当前状态；
- CMDB 资产及凭据类型；
- 当前命令定义；
- 当前设备命令策略。

设备命令已进入黑名单时，不调用执行器，将提案转为 `REJECTED` 并记录安全原因代码 `policy_blacklisted`。动态凭据仍要求本次请求提供一次性密码，密码不落库。

策略复检和 `APPROVED -> EXECUTING` 认领位于同一短事务中。认领使用带旧状态条件的数据库原子更新，不能只依赖进程内 `asyncio.Lock` 或 SQLite 无效的行锁。认领成功后立即提交，随后才允许调用设备或通知执行器。

### 4.3 外部执行与事务边界

HITL 执行使用独立数据库会话，不复用聊天 loop 的 transcript 事务：

1. 短事务完成策略复检、原子认领和 `EXECUTING` 提交。
2. 事务外调用设备或通知执行器。
3. 明确成功时用新事务写 `EXECUTED`。
4. 调用开始后出现任何不能证明“命令尚未发送”的失败时，用新事务写 `UNKNOWN`。

人工审批 API 先提交 `APPROVED`，再调用独立执行服务。自动审批的聊天钩子也通过同一执行服务完成，避免 API 路径和 Agent 路径出现两套安全语义。

新增：

```text
POST /api/v1/hitl/proposals/{proposal_id}/resolve-unknown
body: { "resolution": "confirm_executed" | "allow_retry" }
permission: agent:hitl_approve
```

两种处置均写操作审计和安全 WebSocket 事件。现有 retry API 只接受 `APPROVED`；`UNKNOWN` 必须先人工转回 `APPROVED`。

## 5. 阶段一：根会话串行化与 transcript 完整性

### 5.1 Turn 租约

`agent_sessions` 增加：

- `active_turn_token`：当前请求生成的不可预测 UUID；空值表示没有 turn 在运行。
- `active_turn_started_at`：认领时间。

POST 消息流程：

1. 验证会话所有权。
2. 以 `active_turn_token IS NULL` 为条件原子认领会话并提交。
3. 认领失败返回 `409`，不保存第二条用户消息。
4. 认领成功后保存并提交用户消息。
5. 执行 Agent turn。
6. 完成或失败时只允许持有相同 token 的请求释放租约。

服务启动时清理遗留 turn 租约。由于项目明确只支持单 worker，启动清理不会影响另一个受支持的运行实例。

### 5.2 完整消息单元

Agent loop 不再先持久化 `assistant(tool_calls)` 再调用工具。一个工具步骤按以下顺序处理：

1. 模型返回 assistant tool calls。
2. 调度全部工具并收集 `ToolResult`。
3. 将 assistant tool-call 消息和所有对应 tool result 作为完整单元写入。

如果调度期间发生未处理异常，完整单元尚未写入，因此不会留下悬空 tool call。用户消息已经独立提交，可以保留；API 回滚本 turn 未完成的 transcript，再写安全错误事件。已经由独立 HITL 执行服务持久化的提案仍可从会话快照恢复。

## 6. 阶段二：上下文压缩

压缩不再直接以固定原始行数作为切点。系统先把消息划分为不可拆分单元：

- 普通 user 或无工具调用的 assistant 消息各自为一个单元。
- `assistant(tool_calls)` 与所有匹配 `tool_call_id` 的 tool result 组成一个工具单元。
- 不完整工具单元不能进入摘要，也不能推进 `compacted_through_message_id`。

当最近 16 条原始消息的边界落在工具单元内部时，边界向前移动，把整个工具单元保留在近期原始窗口。摘要请求因此始终满足 OpenAI 兼容消息顺序。`build_model_history` 保留丢弃开头孤立 tool 消息的防御性检查，但正常压缩不应再触发它。

测试至少覆盖单工具、多工具、边界正好位于 assistant/tool 之间、不完整工具调用、连续多轮压缩和摘要失败不推进游标。

## 7. 阶段二：会话快照、分页与前端恢复

### 7.1 安全快照 API

新增会话所有者可访问的快照接口：

```text
GET /api/v1/agent/sessions/{session_id}/snapshot
query: before_message_id?, limit?
permission: agent:use + session owner
```

响应包含：

- 一页根 Agent 消息，按 ID 升序供前端渲染；
- `has_more_messages` 和 `next_before_message_id`；
- 当前非终态 HITL 提案的安全摘要；
- 当前及有限数量最近终态子 Agent 的安全回执。

HITL 安全摘要只包含提案 ID、动作类型、状态、安全原因、资产 ID 和时间字段。完整 `action_payload` 仍只通过现有 `agent:hitl_approve` 接口读取。动态密码永不出现在快照。

消息查询使用 cursor，不再无上限返回全部历史。默认和最大 limit 在后端固定；前端向上滚动时使用 `next_before_message_id` 加载更早页。

### 7.2 前端恢复规则

前端在以下时机同步快照：

- 首次进入会话；
- 切换会话；
- WebSocket 从断开恢复为已连接；
- POST 消息请求完成，无论期间是否错过流式事件。

请求使用 `AbortController` 或单调递增请求序号。响应落地前必须确认 session ID 和请求序号仍为当前值，旧会话响应不能覆盖新会话。

Reducer 按服务端消息 ID、proposal ID 和 child ID 去重。快照替换服务端可恢复状态，但保留仍在发送中的本地 optimistic 用户消息。最终 assistant 消息以数据库快照为准，WebSocket delta 只用于即时显示。

待审批、`EXECUTING` 和 `UNKNOWN` 卡片刷新后仍可见。`UNKNOWN` 卡片向有权限管理员提供两个人工处置动作；无权限用户只能查看安全状态。

“完全访问”确认框保存打开时的目标 session ID。确认时只能修改该 session；切换会话自动关闭旧确认框。

## 8. 阶段三：自动 Spawn 编排

### 8.1 根 Agent 工具

根 Agent 增加：

- `spawn_agent(role, task_brief)`
- `wait_agent(child_id, timeout_ms)`
- `list_agents()`
- `close_agent(child_id)`

LLM 只选择受支持角色并提供任务说明。模型、工具白名单、沙箱、预算、并发数和最大深度全部由服务端现有角色与配置决定，不暴露可绕过限制的参数。默认固定 `fork_mode="none"`，根 Agent 必须在 task brief 中提供完成任务所需的最小上下文。

根提示词规定：

- 简单资产查询和单设备探活直接使用根工具，不 Spawn。
- 至少两个互相独立的调查子问题才适合并行 Spawn。
- 子 Agent 只执行资产、监控和知识库等只读调查。
- 子 Agent 不拥有通知、设备命令、审批或再次 Spawn 权限。
- 根 Agent 等待所需结果并负责最终汇总。
- 涉及变更时，子 Agent 只能调查风险，最终命令由根 Agent 经过 HITL 发起。

### 8.2 生命周期展示与恢复

SpawnManager 发布安全生命周期事件：创建、运行、完成、失败、取消和关闭。事件只包含 child ID、角色、简短任务说明、状态和安全结果摘要。

前端展示只读子任务卡片，不提供手动创建、等待或关闭按钮。WebSocket 用于实时更新，刷新或断线后由会话快照中的持久化 registry 回执恢复。

子任务失败不会阻止根 Agent 汇总其他已完成结果。超时只结束本次 wait，不隐式取消仍在运行的子 Agent。进程重启时按现有恢复策略把失去运行时所有权的子任务关闭，并标记基础设施失败。

### 8.3 单 Worker 约束

本次不实现多 worker Spawn。部署文档和示例命令明确固定 `workers=1`；启动时检查已知的 worker 配置来源，对大于 1 的配置直接失败并给出中文说明。文档明确多个独立应用实例同样不受支持。

## 9. 阶段四：WebSocket 背压

每个 WebSocket 连接拥有独立的有界发送队列和 writer task：

- 广播只做非阻塞入队，不逐个等待网络发送。
- writer task 负责该连接的 `send_json`。
- 队列满、发送超时或连接异常时，只断开并清理该慢连接。
- 连接关闭时取消 writer task 并释放队列，清理操作可重复执行。

测试使用一个永久阻塞的假连接和一个正常连接，验证慢连接不会延迟正常连接，也不会阻塞模型 delta 回调。

## 10. 阶段四：前端分包与静态质量

使用 React `lazy` 和现有 Vite 路由能力拆分运维助手、CMDB、监控和系统管理页面，不新增依赖。验收时入口主包不再触发 Vite 的 500 KB 警告；若单个页面自身仍过大，只针对该页面已有大型依赖做最小拆分。

修复审查发现的全部静态检查问题：

- Ruff 18 项。
- mypy 16 项。
- ESLint 2 个错误和 9 个警告。

只处理直接导致检查失败或警告的未使用导入、类型收窄、类型声明、React hook 依赖和模块导出，不做无关重构。

## 11. 文档同步

更新：

- `docs/AGENT_ARCHITECTURE.md`：新 HITL 状态机、turn 租约、完整消息单元、压缩边界和自动 Spawn。
- `docs/DEPLOYMENT.md`：单 worker 强制约束、启动恢复行为和 WebSocket 背压。
- `docs/guide.md`：`UNKNOWN` 的管理员检查与处置流程、刷新恢复和子任务状态说明。
- 相关 Mermaid 状态图或时序图：审批、执行认领、断线恢复和根/子 Agent 数据流。

## 12. 测试与验收

阶段一验收：

- 提案创建后策略变为黑名单，审批或重试都不能调用设备执行器。
- 两个并发审批请求只有一个能认领执行。
- `EXECUTING` 在设备调用前已经提交。
- 模拟调用后崩溃，启动恢复为 `UNKNOWN`，自动重试被拒绝。
- 管理员可以确认已执行或允许重试，两种操作均有审计。
- 同一会话并发 POST 只有一个执行，另一个返回 `409`。
- 工具调度异常不会留下悬空 tool call。

阶段二验收：

- 压缩永不拆分 assistant tool call 与 tool result。
- 快速切换会话不会显示旧会话历史。
- 刷新和 WebSocket 重连后恢复最终回答、待审批卡片和 `UNKNOWN` 卡片。
- 消息通过 cursor 分页，初次加载不返回完整超长历史。
- 完全访问确认只能修改打开确认框时的会话。

阶段三验收：

- 根 Agent 可以自动创建多个只读子任务、等待并汇总。
- 子 Agent 不能调用设备变更、HITL 或 Spawn 工具。
- 预算、深度、并发、取消、超时和重启恢复限制继续生效。
- 子任务事件可实时显示，并可从快照恢复。

阶段四及最终验收：

- 慢 WebSocket 客户端不阻塞正常客户端。
- 前端生产构建不再报告入口主包超过 500 KB。
- `uv run pytest -q` 全部通过，包括新增测试。
- `uv run ruff check app tests` 零错误。
- `uv run mypy app` 零错误。
- `npm test`、`npm run typecheck`、`npm run lint`、`npm run build` 全部通过。
- Alembic 只有一个 head，升级和降级迁移测试通过。
- Git 工作区干净，所有提交都位于 `master`，不执行 push。

## 13. 不在本次范围

- 不实现多 worker 或多实例分布式 Spawn。
- 不承诺网络设备命令的 exactly-once；系统提供的是持久化认领、禁止自动重复和不确定结果人工处置。
- 不为用户增加手动创建或管理子 Agent 的复杂界面。
- 不删除或改写历史审计消息。
- 不借静态检查修复重构无关 CMDB、监控或权限业务。
