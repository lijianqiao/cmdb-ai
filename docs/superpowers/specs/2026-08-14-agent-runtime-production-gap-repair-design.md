# Agent 运行时生产链路缺口修复设计

## 1. 背景与目标

本设计修复 Agent 运行时已经存在实现、类型或前端组件，但生产链路没有接通的八类问题：

1. 前端 WebSocket 解析器丢弃 `child_status`；
2. 探活状态翻转没有发布 `monitor_alert`；
3. 两个批量并行工作流没有根 Agent 工具入口；
4. `SpawnManager.send_input` 没有模型工具出口；
5. reviewer 没有以二级子 Agent 运行；
6. 子 Agent 的预算/墙钟超限没有独立错误标签；
7. 架构文档错误描述了 GC 与 `force_closed` 的关系；
8. HITL 状态图缺少策略复检导致的 `APPROVED -> REJECTED`。

修复目标是让已有能力在生产入口中真实可达，并使测试、运行语义和文档一致。保持现有数据库结构，不新增依赖，不引入资产/网段订阅模型，不调用真实大模型或真实设备。

## 2. 总体边界

- 所有改动直接落在现有 `master`，不创建分支。
- WebSocket 仍是低延迟通知通道，数据库和快照仍是权威状态来源。
- 监控告警只发给同时具有活跃 Agent WebSocket 连接和 `monitor:read` 权限的用户。
- 两个编排工作流保持只读、只建议，不直接修改知识库分类、CMDB、监控或设备状态。
- `send_input` 仍只允许向当前会话中处于 `RUNNING` 的 child 追加非空消息。
- reviewer 的最大嵌套深度仍为 2，只有 reviewer 可以作为嵌套角色。
- `AgentTraceEvent.error_class` 的现有字符串列足以容纳新标签，不做迁移。

## 3. 前端 `child_status` 实时更新

`AgentWsEventType` 和 reducer 已支持 `child_status`，缺口仅位于 `frontend/src/lib/agent-ws.ts` 的运行时白名单。

修复方式：

- 把 `child_status` 加入 `AGENT_WS_EVENT_TYPES`；
- 在解析器单测中通过真实 `parseAgentWsMessage` 输入一条 `child_status` 文本帧，断言返回判别式消息；
- 保留 reducer 现有处理逻辑，不复制状态归并代码。

该测试必须能在删除白名单项时失败，避免再次出现“类型声明和 reducer 都正确，但解析层静默丢帧”的假通过。

## 4. `monitor_alert` 发布链路

### 4.1 翻转判定与事务边界

`run_monitor_sweep_once` 在探测前批量读取各目标的最新状态。只有目标已有历史状态且本次状态不同，才记录一条待发布告警；首次探测不属于状态翻转。

持久化继续遵循现有语义：同状态探测更新当前状态行的检查时间、延迟和详情，状态翻转才追加新行。所有探测结果和清理操作提交成功后，才发布本轮收集的告警。发布失败只记录服务日志，不回滚已经提交的监控事实，也不阻断同轮其它告警。

告警 payload 只包含展示所需的非凭据字段：

- `target_id`、`asset_id`、`asset_name`、`ip_address`、`port`；
- `previous_status`、`status`、`latency_ms`、`checked_at`；
- 前端现有横幅使用的 `title`、`message`、`severity`。

不发送探测异常原文、凭据或其它内部配置。

### 4.2 权限过滤广播

项目当前没有资产/网段订阅表、API 或前端订阅设置，因此本次不伪造“订阅”语义。采用第一期的安全最小实现：向所有具备 `monitor:read` 的在线 Agent WebSocket peer 广播状态翻转。

实现边界：

- WebSocket 建连时除 `agent:use` 外，独立计算 `monitor:read` 能力并写入 Hub peer 元数据；没有 `monitor:read` 不影响聊天连接本身；
- 周期性重新鉴权时刷新该能力，使权限撤销沿用现有最长 60 秒的连接复检窗口；
- Hub 新增监控广播入口，只给 `can_read_monitor=true` 的 peer 入队，继续复用每连接有界队列、慢连接清理和发送超时；
- `monitor_sweep` 通过一个可注入的发布器调用 Hub，单元测试使用内存 fake，不启动真实 WebSocket。

文档同步改为“按 `monitor:read` 权限广播给在线 Agent 会话”。资产/网段级订阅属于独立产品功能，不在本次范围内。

## 5. Spawn 原语与确定性编排入口

### 5.1 工具集合

根 Agent 工具面拆成两个有明确语义的集合：

- 五个 Spawn 原语：`spawn_agent`、`wait_agent`、`send_input`、`list_agents`、`close_agent`；
- 两个只读编排工具：`classify_documents`、`investigate_root_cause`。

`SPAWN_TOOL_NAMES` 继续表示同一个 dispatcher 负责的工具全集，供 `chat_turn` 路由；另设原语集合与编排集合，避免把七个工具错误描述成“七个 Spawn 原语”。

### 5.2 `send_input`

工具参数为 `child_id` 和长度受限的非空 `message`。dispatcher 先复用当前会话 child 归属校验，再调用 `SpawnManager.send_input`，返回更新后的安全回执。`SpawnRejectedError`、child 不存在和参数错误继续映射为固定的模型可见控制结果，不暴露内部异常。

已有 manager 层测试保留；新增工具 schema、会话隔离和 RUNNING/终态行为测试，证明模型入口真实可用。

### 5.3 两个编排工具

`classify_documents` 接受至少两份结构化文档元数据和可选候选分类；`investigate_root_cause` 接受非空事故上下文和至少两个可选调查分支。参数由严格 Pydantic schema 校验，session ID 仍由服务端绑定，模型不能覆盖。

dispatcher 调用现有 `orchestration.py` 工作流，并把不可变 outcome 序列化为 JSON 工具结果。结果保留建议、证据缺口、失败 child、解析失败和 reviewer 总结，但不执行分类写入或变更操作。根循环提示词明确：匹配批量分类或多分支根因排查时优先调用编排工具，只有不满足工作流前置条件时才使用底层原语。

这样仍由根模型判断用户意图，但一旦选择工作流，分波并发、严格结果解析、复核条件和清理由服务端代码确定，不再让模型手工复刻两张时序图。

## 6. 二级 reviewer 生命周期

`_run_wave` 仍在进入下一波前关闭前一波，但把最后一波的终态回执及其关闭责任交给工作流。工作流解析所有结果、决定是否需要 reviewer 后，先关闭最后一波中不再需要的回执；如果不需要 reviewer，则关闭整波。

需要 reviewer 时，在以下条件同时满足时保留一个已终态但尚未关闭的成功 worker 作为 reviewer 父节点：

- 工作流需要 reviewer；
- 最后一波至少有一个成功 wait 的 worker；
- `max_concurrent_children >= 2`，保留父节点后仍有 reviewer 槽位。

reviewer 创建前先关闭最后一波的其它回执，释放至少一个并发槽；随后传入保留 worker 的 `child_id` 作为 `parent_agent_id`，形成 `root -> worker -> reviewer`。reviewer 必须先关闭，随后在 `finally` 中关闭保留的父 worker；取消和异常路径继续使用 shielded cleanup，不能泄漏并发槽。

以下情况回退为根级 reviewer，以保留现有功能而不死锁：

- 配置只有一个并发槽；
- 最后一波没有可用父 worker；
- 父节点在 reviewer 创建前已不可用。

默认并发配置为 5，因此正常生产工作流会实际产生深度 2。回退路径必须在 outcome 中保持原有成功/失败语义，不能因为无法嵌套而丢失已获得的调查或分类结果。

## 7. 子 Agent 错误分类

`ChildErrorClass` 和所有安全白名单扩展为五类：

```text
model | tool | policy_reject | infra | budget_exceeded
```

映射规则固定为：

| 运行结果 | child 状态 | `error_class` |
| :--- | :--- | :--- |
| 正常 final answer | `COMPLETED` | `null` |
| step/cost 超限导致 `LoopOutcome.reason=budget_exceeded` | `FAILED` | `budget_exceeded` |
| child 墙钟超时 | `FAILED` | `budget_exceeded` |
| `LoopOutcome.reason=llm_error` | `FAILED` | `model` |
| 连续工具失败、clarification/rejected 等 `early_exit` | `FAILED` | `policy_reject` |
| 工具调用异常逃逸 | `FAILED` | `tool` |
| 运行时、持久化或所有权异常 | `FAILED`/`CANCELLED` | `infra` |

该调整只改变分类标签，不改变预算计数、取消、终态写入或重试策略。

## 8. 文档语义修正

### 8.1 GC 与 `force_closed`

后台 receipt GC 只扫描超过 TTL 且已经处于 `COMPLETED`、`FAILED` 或 `CANCELLED` 的回执，将其正常关闭并释放本进程持有的槽位，`force_closed=false`。GC 不取消仍在运行的 child。

只有显式 `close_agent` 取消本进程拥有的运行 task，且宽限期结束后 task 仍未退出，才强制 detach 并写 `force_closed=true`。启动 reconciliation 关闭孤儿行时也保持 `false`。

### 8.2 HITL 黑名单复检边

状态机增加真实存在的边：

```text
APPROVED --preflight: policy_blacklisted--> REJECTED
```

命令不存在或动态凭据缺失等其它预检失败仍不认领，提案保持 `APPROVED`；只有策略在执行前变为黑名单时，原子地把 `APPROVED` 转为 `REJECTED` 并记录安全原因 `policy_blacklisted`。

同步修正架构文档、运行时可靠性设计和 guide 中的状态图、错误分类、监控广播、Spawn 原语及 reviewer 描述。历史实施计划不作为运行时契约，不反向改写已经完成的逐步操作记录。

## 9. 测试与验收

测试严格按失败测试先行：

1. 前端解析器测试证明 `child_status` 不再返回 `null`；
2. monitor sweep 测试覆盖首次探测不广播、同状态不广播、翻转在 commit 后广播、发布失败不撤销监控事实；
3. Hub/API 测试覆盖 `monitor:read` peer 收到告警、无权限 peer 收不到、周期复检刷新能力；
4. Spawn 工具测试覆盖 `send_input` 和两个编排工具的 schema、dispatcher、会话隔离及结构化结果；
5. 编排单元与真实 SpawnManager 集成测试覆盖默认配置产生二级 reviewer、单并发回退、异常/取消不泄漏槽位；
6. child runner 测试分别覆盖预算超限、墙钟超时、`llm_error` 和 `early_exit` 的标签；
7. 运行相关后端测试、完整后端测试、前端测试、前端构建与项目现有静态检查。

验收时不启动真实模型请求、不连接真实设备、不 push。所有测试通过后，把代码、测试和文档作为独立于本设计提交的实现提交保留在本地 `master`。
