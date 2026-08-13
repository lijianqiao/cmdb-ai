# 会话审批三档 Design Spec

**状态**：已与项目所有者确认设计（2026-08-13）。

**范围**：只做「当前会话」的审批模式。设备连接复用（一场对话对某台设备先登录、root/子 Agent 共用连接）是**下一份独立 spec**，本期不做。

## 1. 目标与背景

系统设置里的「notify 自动审批」是全站开关：打开后**所有会话**的 `notify` 提案都会自动批准。主流 Agent 工具（Cursor / Claude Code）是**当前对话**选择「请求审批 / 帮我审批 / 完全访问」，而不是管理员替所有人决定。

本设计把审批模式落到 `AgentSession` 上，删掉全站开关（包括旧库里的配置键，不留尸）。设备命令仍走现有黑白名单与动态凭据规则，但**是否自动批准**由当前会话档位决定。

## 2. 复用点（不重新发明）

| 复用对象 | 现状 | 本设计的用法 |
| :--- | :--- | :--- |
| `AgentSession` | `id / user_id / title / status` | 加一列 `approval_mode`，默认 `ask` |
| `POST/GET /api/v1/agent/sessions` | 已有创建、列表、详情；**尚无 PATCH** | 列表/详情带出档位；**新增** `PATCH /sessions/{id}` 只改档位（仍走 `agent:use` + 所有者校验） |
| `_owned_session_or_404` | 非所有者一律 404 | PATCH 复用，不新建权限码 |
| `propose_action` 自动批准分支 | `notify` 看 `operations.hitl_notify_auto_approve`；设备白名单+非动态凭据一律当场执行 | 改为读该 `session_id` 的 `approval_mode`；会话不存在则拒绝提案；不再读取系统配置该项 |
| `list_device_commands_for_asset` | 文案写死「白名单（自动执行）」「需人工审批」，且工具函数目前不接收 `session_id` | 必须按当前会话档位改文案（见 §3.2）；`tool_dispatch` 已有 `session_id`，传进去 |
| 黑名单 / 白名单 / 未分类 | `device_command_policy_crud.resolve_policy` 返回 `blacklist` / `whitelist` / `None` | 判定表继续用这三个结果；黑名单仍在建提案前硬拒绝 |
| 动态凭据批准密码 | `HitlApprovalCard` 已有密码框；明文不落库 | 本期不改 OTP 交互；三档都不能在没密码时自动执行动态凭据 |
| `log_audit` | 写操作与审计同一事务 | 改档记 `update_session_approval_mode` |
| `init_db` 种子 | 幂等写入运行配置键 | **幂等删除** `HITL_NOTIFY_AUTO_APPROVE`，并不再写入 |
| `ChatInput` / 侧栏会话列表 | 输入框 + 左侧会话列表 | 输入框左侧切档；侧栏每条显示当前档位中文 |

## 3. 三档语义

取值固定三个英文字符串（存库、接口用英文；界面用中文）：

| 值 | 界面文案 |
| :--- | :--- |
| `ask` | 请求审批 |
| `assist` | 帮我审批 |
| `full` | 完全访问 |

新会话、以及迁移后已有会话，全部默认 `ask`。

`propose_action` 以**创建提案那一刻**读到的 `approval_mode` 为准。对话中途改档：

- 已经 `PENDING` 的卡片不自动批准、不自动消失
- 之后新产生的提案跟新档走

同一场对话允许从请求审批改成帮我审批（或完全访问），输入框旁的选择器在已选中会话时**始终可改**，不会因为已经选过某一档而禁用。

### 3.1 判定表

黑名单在建提案之前拒绝，三档相同，不给人「批准一条禁止执行的命令」的机会。

| 模式 | notify | 黑名单命令 | 白名单 + 非动态凭据 | 未分类 + 非动态凭据 | 动态凭据（非黑名单） |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `ask` | 弹卡 | 拒绝 | 弹卡 | 弹卡 | 弹卡，批准时填本次密码 |
| `assist` | 自动过 | 拒绝 | 当场执行 | 弹卡 | 弹卡，批准时填本次密码 |
| `full` | 自动过 | 拒绝 | 当场执行 | 当场执行 | 弹卡，批准时填本次密码 |

要点：

- **`ask` 比今天更严**：今天白名单设备命令不看 notify 开关就会当场执行；改完后默认 `ask`，白名单也要人批。
- **`assist` 接近今天「打开 notify 自动审批」之后的设备行为**：notify 自动过，白名单当场执行，未分类弹卡。
- **`full` 只多放开「未分类」**：仍然拒绝黑名单；动态凭据仍然要人输入密码。
- 子 Agent 发起的提案挂在同一个 `session_id` 上，**共用父会话的档位**，不给 child 单独设档。
- `credential_type == "none"` 仍在策略判定之前就被 `propose_action` 拒绝（「未配置登录凭据」），不进本表。

自动批准时仍走现有 `decide_proposal(..., reviewed_by_user_id=actor_user_id)` + `resume_proposal`，审计行为与今天自动批准分支一致。

### 3.2 给模型看的策略文案必须跟档位一致

`list_device_commands_for_asset` 今天对白名单固定写「自动执行」、对未分类固定写「需人工审批」。默认改为 `ask` 之后，这两句会对模型撒谎（白名单也会弹卡）。

工具改为接收 `session_id`，读出 `approval_mode` 后再写策略句：

| 策略 | `ask` | `assist` | `full` |
| :--- | :--- | :--- | :--- |
| 黑名单 | 禁止执行 | 禁止执行 | 禁止执行 |
| 白名单 | 白名单（当前为请求审批，需人工批准） | 白名单（可自动执行） | 白名单（可自动执行） |
| 未分类 | 未分类（需人工审批） | 未分类（需人工审批） | 未分类（完全访问，可自动执行） |

动态凭据那句「所有命令都需要人工审批并当场输入密码」三档都保留。

## 4. 数据与迁移

`agent_sessions` 增加：

```text
approval_mode VARCHAR(20) NOT NULL DEFAULT 'ask'
```

应用层只接受 `ask | assist | full`（Pydantic `Literal`）。不做数据库 CHECK 约束，与现有 `status` 列风格一致。

Alembic 迁移：加列 + server_default `'ask'`，已有行全部变成 `ask`。

`AgentSessionCreate` **不接收** `approval_mode`，避免创建时直接开完全访问；要开必须走 PATCH（完全访问还有前端警告）。

## 5. 接口

权限：现有 `agent:use`。能进这场对话 = 会话 `user_id` 等于当前用户（与发消息、删会话相同）。**不要求** `agent:hitl_approve`，**不新增**权限码。

### 5.1 响应带档位

`AgentSessionResponse`（及前端 `AgentSession`）增加 `approval_mode: "ask" | "assist" | "full"`。创建、列表、详情都返回。

### 5.2 改档

当前会话路由**没有 PATCH**，本期在同一文件增加：

```text
PATCH /api/v1/agent/sessions/{session_id}
权限：agent:use
body: { "approval_mode": "ask" | "assist" | "full" }
```

- 非所有者或不存在 → 404（与 GET/DELETE 一致，避免枚举）
- 缺字段或非法值 → 422
- 成功 → 200，返回更新后的会话；`log_audit`：
  - `action`: `update_session_approval_mode`
  - `target`: `agent_session:{id}`
  - `detail`: `旧档→新档`（例如 `ask→full`），不含密码
- 改成相同档位也允许（幂等），**不写审计**（没有实际变更不刷操作日志）

前端操作日志 `ACTION_LABELS.update_session_approval_mode = "变更会话审批模式"`。

不为此单独推 WebSocket 事件。PATCH 成功后必须同时更新：输入框选择器、当前会话对象、侧栏列表里对应那一项，三处档位一致。

## 6. 前端

### 6.1 输入框旁切换

`ChatInput` 左侧增加 `Select`（`SelectItem` 必须包在 `SelectGroup` 里），三项中文文案见第 3 节。图标只从 `@/lib/icons` 引入。

- 没有选中会话：选择器禁用
- 已选中会话：始终可改，不因「已经选过」而禁用
- 受控值来自当前会话的 `approval_mode`；在完全访问的确认 Dialog 关闭前，选择器仍显示旧档，禁止先把 UI 改成 `full` 再等确认

改档请求由 `OpsAssistantPage` 发起（`ChatInput` 只回调选中的档位），避免输入框自己打 API。

### 6.2 完全访问警告

从非 `full` 改为 `full` 时：

1. 先弹出 `Dialog`（`DialogTitle` 必填：「确认开启完全访问」）
2. 正文：未分类的设备命令将不再询问你；黑名单仍然拒绝；动态凭据仍要你输入本次密码。此设置只对当前对话有效。
3. 取消：不发请求，选择器保持旧档
4. 确认：再 `PATCH`；失败 toast，保持原档
5. 成功后再把本地会话、侧栏该项、选择器改成 `full`

从 `full` 改回 `ask` / `assist`：不警告，直接 PATCH。

### 6.3 侧栏显示档位

左侧会话列表每条在标题下方、时间旁边（或时间下一行）显示当前档位中文。用户换会话时能看出「这场是请求审批、那场是帮我审批」。列表数据来自已有的会话列表接口，无需额外请求。

## 7. 全局开关下线（删干净）

删除，而不是停用后留键：

- `Settings.HITL_NOTIFY_AUTO_APPROVE`
- `.env.example` 中对应行
- `KEY_HITL_NOTIFY_AUTO_APPROVE`、`EffectiveOperationsConfig.hitl_notify_auto_approve`、读写映射、PUT 审计字段
- 前端运行参数 schema / `OperationsConfigCard` 的 Switch 与文案
- `docs/SYSTEM_CONFIG.md`、`backend/README.md`、`backend/README_zh.md` 中的该项
- `docs/AGENT_ARCHITECTURE.md` 第 6、9 节：L3 改为「会话三档」；写明默认 `ask`、黑名单不可绕过、动态凭据始终要人输入；不再写「notify 全站可配置自动批准」

`init_db`：

- `_system_config_seed_values()` **不再包含**该键
- 种子过程**幂等 DELETE** `system_configs` 中 `key = HITL_NOTIFY_AUTO_APPROVE` 的行
- 运行参数有效键从 5 个变为 4 个：探活超时、巡检间隔、CMDB 对账间隔、监控日志保留天数

旧测试里 `monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", ...)` 全部改为给测试会话设置 `approval_mode`。原先「白名单自动执行」用例必须把会话设为 `assist` 或 `full`，否则会按新默认 `ask` 变成 PENDING——这是预期。

## 8. 测试要点

- 创建会话：`approval_mode == "ask"`
- PATCH：所有者成功；他人 404；非法值 422；`full` 可写库（警告只在前端）
- HITL：`ask` 下 notify 与白名单均为 PENDING；`assist` 下 notify 自动过、白名单执行、未分类 PENDING；`full` 下未分类也执行；三档黑名单均拒绝且不建待批；动态凭据在 `assist`/`full` + 白名单下仍 PENDING
- `list_device_commands_for_asset`：同一条白名单命令，在 `ask` 会话下的返回文本不得再写「自动执行」
- 系统配置：种子后无 `HITL_NOTIFY_AUTO_APPROVE`；运行参数表单/API 不再出现该字段
- 改档审计：有变更才有 `update_session_approval_mode`

## 9. 明确不做（下一份 spec）

- 一场对话对某台设备先 OTP 登录、后续命令复用同一条 SSH/CLI 连接
- 该连接按 `(session_id, asset_id)` 供 root 与所有子 Agent 共享，并对同一资产的命令加锁串行
- 把审批卡片上的动态密码框改成独立 OTP 弹窗

## 10. 风险

- 默认 `ask` 会改变现网体感：以前白名单 `show` 类命令可能直接跑，现在新对话每条都要批。这是所有者选择的「最安全默认」，产品上要在选择器里让「帮我审批」好找。
- 完全访问不校验 `agent:hitl_approve`：能用助手的人就能对自己的对话放开未分类命令。黑名单与动态凭据仍是硬闸门。
- 自动批准分支今天会先推 `hitl_pending` 再立刻批准执行；本期保持该顺序，不单独为三档改事件时序。
