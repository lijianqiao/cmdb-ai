# 设备命令执行能力 Design Spec

**状态**：已与项目所有者确认设计（2026-08-12），待写实施计划。

## 1. 目标与背景

运维助手目前能查 CMDB（`query_cmdb`/`query_cmdb_dependencies`）、查监控（`query_monitor_status`），也能提整改提案（`propose_remediation` → HITL → `device_control` 执行器，但执行器还是 `NotImplementedExecutor` 占位）。用户实际使用时发现：让助手去看某台设备的运行配置，助手会如实回答"没有 SSH 工具、不知道怎么连"——这不是模型能力问题，是当前工具清单里压根没有任何能连接设备、跑命令的工具（详见 `docs/AGENT_ARCHITECTURE.md` §4.2 的完整工具清单和 A6 假设：设备管控通道何时接入是刻意留白的后续工作）。

本设计补上这条通道的**只读诊断命令**部分：Agent 能对 CMDB 里配置了登录凭据的资产发起命令查询，命中白名单的直接执行，没有策略记录的走现有 HITL 人工审批，命中黑名单的直接拒绝。

**不在这次范围内**：`device_control`（reboot/shutdown/port_disable/port_enable）本身不受影响，仍是独立的 action_type、独立的 stub 执行器；本设计新增的命令目录里可以包含会改变设备状态的命令（见 §4），但那是"任意只读诊断之外，管理员额外收录并明确管控的少量命令"，不等于开放通用命令执行。

## 2. 复用点（不重新发明的部分）

这个设计几乎完全长在已有基础设施上：

| 复用对象 | 现状 | 本设计的用法 |
| :--- | :--- | :--- |
| `HitlProposal` 状态机（`app/agent/hitl.py`） | `PENDING → APPROVED/REJECTED → EXECUTED`，`propose_action`/`decide_proposal`/`resume_proposal` 三段式，`action_type: Literal["notify","device_control"]` | 加第三个 `action_type = "device_query"`，走同一条状态机、同一套 `_validated_payload`/`_summary`/`_publish` |
| 自动批准分支 | `propose_action` 里 `if action_type == "notify" and settings.HITL_NOTIFY_AUTO_APPROVE:` 立即 `decide_proposal(approve=True)` + `resume_proposal` | 复制这段逻辑的结构，条件换成"命中白名单策略" |
| 执行器接口 | `DeviceControlExecutor` Protocol + `NotImplementedExecutor` stub（`app/agent/executors.py`） | 新增 `DeviceQueryExecutor`，同样的 `ExecutionResult(ok, message, detail)` 返回契约 |
| WS 安全事件 | `AgentWsServerMessage`（`app/schemas/agent_ws.py`）、`WsHitlEventPublisher`（`app/agent/ws_hub.py`）只透传 `ProposalSafeSummary` 白名单字段 | 给 `ProposalSafeSummary` 加一个可选字段，走同一条 publish 路径 |
| 审批卡片 UI | `HitlApprovalCard.tsx`：渲染在聊天消息流里，`canApprove` 才拉完整 payload，批准/拒绝走 `decide_proposal` API | 加一个"执行结果"展示区，`REJECTED`/`EXECUTED` 后展示 `result_excerpt`；`APPROVED` 但资产是动态凭据时，批准按钮旁多一个密码输入框 |
| CMDB 凭据 | `CmdbAsset.credential_type/credential_username/credential_password_encrypted`，`app/core/cmdb_credential.py` 的 `encrypt_credential_password`/`decrypt_credential_password` | 直接读，不改字段 |
| 根 Agent 专属工具收紧 | `build_root_tool_dispatcher` 才挂 `propose_remediation`，子 Agent 的 `build_tool_dispatcher` 不给（`test_hitl_integration.py::test_scenario_c_child_dispatcher_rejects_propose_remediation` 已验证） | 新工具同样只挂根调度器 |
| 角色目录 read-only 边界 | `app/agent/roles.py`：`ops_explorer`/`investigator`/`reviewer` 只有 `_OPS_TOOLS`（三个只读查询） | 不改子角色白名单，`query_device_command`/`get_device_query_result` 不进任何子角色的 `tools_allowlist` |

## 3. 数据模型

### 3.1 `HitlProposal.action_type` 扩展

```python
type ActionType = Literal["notify", "device_control", "device_query"]
```

`device_query` 的 `action_payload`（写入前经 `_validated_payload` 校验，跟 `notify`/`device_control` 一样有专门的 Pydantic 模型）：

```python
class DeviceQueryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command_name: str = Field(min_length=1, max_length=100)
```

`asset_id` 仍然走顶层合并规则（`_validated_payload` 已有的 `payload.asset_id` 与顶层一致性校验，不用改）。

### 3.2 新表 `device_command_policies`

```python
class DeviceCommandPolicy(Base, TimestampMixin):
    __tablename__ = "device_command_policies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)  # "asset_type" | "asset"
    asset_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    asset_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cmdb_assets.id", ondelete="CASCADE"), nullable=True
    )
    command_name: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)  # "whitelist" | "blacklist"
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
```

约束（应用层用 Pydantic `model_validator` 校验，参照 `CmdbAssetCreate`/`Update` 已经建立的"枚举值决定哪些字段必填"模式；数据库层加两条部分唯一索引）：

- `scope == "asset_type"` ⟺ `asset_type` 非空且 `asset_id` 为空。
- `scope == "asset"` ⟺ `asset_id` 非空且 `asset_type` 为空。
- 唯一索引 `(asset_type, command_name) WHERE scope = 'asset_type' AND is_deleted = false`。
- 唯一索引 `(asset_id, command_name) WHERE scope = 'asset' AND is_deleted = false`。

**策略解析顺序（写死在代码里，不可配置）**：先查 `scope="asset"` 且 `asset_id` 匹配的行；没有则查 `scope="asset_type"` 且 `asset_type` 匹配 `CmdbAsset.asset_type` 的行；都没有 → 视为"未分类"（走 HITL）。单台设备的策略永远覆盖设备类型级别的策略，不管方向（白名单可以被单台的黑名单覆盖，反之亦然）——这是唯一的优先级规则，不需要额外的权重/优先级字段。

黑名单命中：`propose_action` 校验阶段直接抛 `HitlProposalRejectedError`，不建 `PENDING` 行（跟"CMDB 资产不存在"这类校验失败的处理方式一致，不是一次正式的、需要留痕的提案，是一次无效请求）。

### 3.3 迁移

一次迁移搞定：`HitlProposal` 不需要改列（`action_type`/`action_payload` 本来就是 `String`/`JSON`，新增枚举值不需要 DDL），只需要新建 `device_command_policies` 表。

## 4. 命令目录（代码层，不入库）

新建 `app/agent/device_commands.py`，结构仿 `app/agent/roles.py` 的 `ROLE_CATALOG`：

```python
type CommandName = Literal[
    "show_version",
    "show_running_config",
    "show_interfaces",
    "ping",
]
type CommandType = Literal["read_only", "state_changing"]

DEVICE_COMMAND_CATALOG_VERSION = "t12-v1"

@dataclass(frozen=True, slots=True)
class DeviceCommandDefinition:
    name: CommandName
    version: str
    description: str          # 给管理员在策略管理页看的说明
    command_type: CommandType
    templates: Mapping[str, str]  # asset_type -> 真实要在设备上跑的命令字符串

_DEVICE_COMMAND_CATALOG: dict[CommandName, DeviceCommandDefinition] = {
    "show_version": DeviceCommandDefinition(
        name="show_version",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="查看设备/系统版本信息",
        command_type="read_only",
        templates={
            "server": "cat /etc/os-release && uname -a",
            "switch": "show version",
            "router": "show version",
            "firewall": "show version",
        },
    ),
    "show_running_config": DeviceCommandDefinition(
        name="show_running_config",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="查看当前生效配置（可能包含敏感信息，建议默认不进白名单）",
        command_type="read_only",
        templates={
            "switch": "show running-config",
            "router": "show running-config",
            "firewall": "show running-config",
        },
    ),
    "show_interfaces": DeviceCommandDefinition(
        name="show_interfaces",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="查看接口状态",
        command_type="read_only",
        templates={
            "switch": "show interfaces status",
            "router": "show interfaces",
            "firewall": "show interfaces",
        },
    ),
    "ping": DeviceCommandDefinition(
        name="ping",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="从设备本机发起连通性测试（固定测试网关，不接受任意目标参数，避免被当探测跳板）",
        command_type="read_only",
        templates={
            "server": "ping -c 4 -W 2 $(ip route | awk '/default/ {print $3}')",
            "switch": "ping <gateway>",
        },
    ),
}
```

`get_device_command(name) -> DeviceCommandDefinition`、`list_device_commands() -> tuple[...]`，跟 `roles.py` 的 `get_role`/`list_roles` 同款失败关闭（未知命令名 → `UnknownDeviceCommandError`，不是 `KeyError`）。

**这是这个设计最核心的安全边界，必须在实施计划里反复强调**：`device_command_policies`（数据库、管理员可改）只决定"这条命令要不要过 HITL"，**不能决定命令内容本身**——真正会在设备上跑的字符串永远来自这个代码层目录，改目录要走代码 review。管理员在白名单页面上能做的，只是从目录已有的命令里勾选"哪些设备/设备类型免审批"，不能凭空发明一条新命令。`ping` 之类接受参数的命令，v1 先不支持自由参数（模板里固定死目标，如上面 `ping` 的例子），避免命令拼接注入——这条限制写进 Out of Scope，作为后续如果要支持参数化命令时的显式待办。

## 5. 触发流程

新工具（`app/agent/hitl_tools.py` 旁边新增，或扩展同文件——实施计划里定）：

```python
async def query_device_command(
    db: AsyncSession,
    *,
    session_id: int,
    actor_user_id: int,
    proposed_by_agent_id: str | None,
    asset_id: int,
    command_name: str,
    reason: str,
    publisher: HitlEventPublisher | None = None,
) -> ToolResult
```

内部调用 `propose_action(action_type="device_query", ...)`。`propose_action` 里新增的校验/分支（`_validated_payload` 之后，创建 `PENDING` 行之前）：

1. 查询 `CmdbAsset`；不存在 → 沿用现有 `HitlProposalRejectedError`。
2. `command_name` 必须在 `device_commands.py` 目录里，且该命令的 `templates` 包含这个资产的 `asset_type` → 否则拒绝（"该资产类型不支持这个命令"）。
3. `asset.credential_type == "none"` → 拒绝（"该资产未配置登录凭据，无法执行设备命令"）——不建提案，因为不管审批多少次都执行不了。
4. 查策略（§3.2 的解析顺序）：
   - `blacklist` → 拒绝，不建提案。
   - 其它情况（`whitelist` 或未分类）→ 正常 `hitl_proposal_crud.create(...)`，`PENDING`。
5. 创建后：
   - 策略是 `whitelist` **且** `asset.credential_type != "dynamic"` → 立即 `decide_proposal(approve=True, reviewed_by_user_id=actor_user_id)` + `resume_proposal(...)`，跟 `notify` 的自动批准分支结构一致。
   - 否则（未分类，或者虽然白名单但资产是动态凭据）→ 停在 `PENDING`，返回 `pending_approval`。

**动态凭据资产永远不会自动执行**，不管策略是不是白名单——没有人在场就拿不到密码，这条在 §6 展开，是本设计里唯一一处"策略说可以，但代码层仍然强制人工介入"的例外，需要在实施计划的测试里专门覆盖（"资产是 dynamic + 命中白名单 → 仍然停在 PENDING"这个组合容易被漏测）。

## 6. 凭据解析与 SSH 连接

新增 `app/agent/executors.py::DeviceQueryExecutor`：

```python
class DeviceQueryExecutor:
    async def execute(
        self,
        db: AsyncSession,
        *,
        asset: CmdbAsset,
        command_name: str,
        dynamic_password: str | None,
    ) -> ExecutionResult: ...
```

- `credential_type == "static"`：`decrypt_credential_password(asset.credential_password_encrypted)` 拿明文。
- `credential_type == "dynamic"`：明文只能来自 `dynamic_password` 参数；为 `None` 时直接 `ok=False`（理论上不应该发生，因为 §5 已经保证动态凭据资产不会走自动执行分支；这里是防御性兜底，不是主路径）。
- 用 `asyncssh` 建连接（新依赖，`uv add asyncssh`；原生 async，跟项目现有全异步架构直接契合，不需要像密码哈希那样包一层 `asyncio.to_thread`），超时参照 `MONITOR_PROBE_TIMEOUT_SECONDS` 的思路新增一个 `DEVICE_COMMAND_TIMEOUT_SECONDS` 配置项。
- 从 `device_commands.py` 目录按 `asset.asset_type` 取真实命令模板，执行，读 `stdout`/`stderr`/退出码。
- 输出截断（参照 T09 `orchestration.py::_truncate` 的做法，截断后加"…(截断)"），塞进 `ExecutionResult.detail = {"output": ..., "truncated": bool}`。
- 连接/认证/超时失败 → `ok=False`，`message` 只给分类信息（如"连接超时""认证失败"），**不把 `asyncssh` 抛出的原始异常文本吐给调用方**，防止把设备侧的错误信息（可能带主机名、内部拓扑细节）意外泄漏给非审批角色能看到的地方——这个顾虑对齐现有 `hitl_tools.py`/`tool_dispatch.py` 里"意外异常仅暴露异常类型"的一贯做法。

`resume_proposal` 签名新增可选参数 `dynamic_password: str | None = None`，只有 `proposal.action_type == "device_query"` 时才会真正用到，透传给 `DeviceQueryExecutor.execute`。

`HitlDecideRequest`（`app/schemas/hitl.py`）新增：

```python
dynamic_credential_password: str | None = Field(default=None, min_length=1, max_length=256)
```

`decide_hitl_proposal` 路由里校验：`body.approve is True` 且提案是 `device_query` 且对应资产 `credential_type == "dynamic"` 时，`dynamic_credential_password` 必填，否则 422；这个值只在这一次 HTTP 请求的调用栈里传递（`decide_hitl_proposal → resume_proposal → DeviceQueryExecutor.execute`），**不写进 `HitlProposal.action_payload`，不写进审计 `detail`，不出现在任何日志里**——用完即弃，跟内存里传递普通函数参数一样，没有任何持久化路径。

## 7. 结果回传到对话

`ProposalSafeSummary`（`app/agent/hitl.py`）新增可选字段：

```python
result_excerpt: str | None = None  # 仅 device_query 执行成功时有值，截断后的命令输出
```

`_summary()` 从 `HitlProposal.action_payload` 或执行结果里取（具体存哪个字段留给实施阶段定，倾向于跟 `NotifyExecutor` 一样，执行成功后把摘要写回 `action_payload` 的一个新增字段，比如 `action_payload["last_result_excerpt"]`，这样 `_summary()` 不用额外查表）。

`AgentWsEventType`/`hitl_resolved` payload 自然带上这个字段（`_HITL_SAFE_KEYS` 加一项）。`HitlApprovalCard.tsx` 在 `status === "EXECUTED"` 时展示这段输出（等宽字体、`max-h` 滚动，参照它已有的"完整 payload"展示区的样式）。

新增只读工具 `get_device_query_result(proposal_id: int) -> ToolResult`：

```python
async def get_device_query_result(
    db: AsyncSession, *, session_id: int, proposal_id: int
) -> ToolResult
```

按 `session_id` 校验提案属于当前会话（复用 `hitl_proposal_crud.get` + 校验 `proposal.session_id == session_id`，不匹配当成"不存在"处理，不泄露其它会话的提案是否存在），返回状态 + `result_excerpt`（没有则说明"还未执行"或"被拒绝"）。这个工具**只读、无审批要求**（跟 `query_cmdb` 同一档），因为它读的是已经走完 HITL 流程、已经产生的结果，不是发起新的设备访问。

这样两条路径都能让 Agent 在对话里把结果讲给用户：白名单自动批准时，`resume_proposal` 就在同一次工具调用里跑完，`propose_remediation`/`query_device_command` 的工具包装函数直接把 `summary.result_excerpt` 塞进当轮的 `ToolResult.content` 返回给模型；需要人工审批的场景，用户批准（可能还要输入动态密码）之后，Agent 要在后续轮次用 `get_device_query_result` 主动回查。

## 8. 权限与前端管理页

新增权限码（`init_db.py` 的 `SEED_PERMISSIONS`）：

| code | module | 用途 |
| :--- | :--- | :--- |
| `device_command_policy:read` | 设备命令策略 | 查看白/黑名单列表 |
| `device_command_policy:manage` | 设备命令策略 | 增删改白/黑名单条目 |

前端新增页面 `frontend/src/pages/DeviceCommandPoliciesPage.tsx`，结构照抄 `PermissionsPage.tsx`（表格 + 新增/编辑弹窗 + 回收站，弹窗里 `scope` 选择后动态显示 `asset_type` 下拉或者 `asset_id` 的资产选择器，`command_name` 下拉列表来自后端暴露的目录只读端点，不是自由输入）。挂进侧栏"运维管理"分组（CMDB 资产旁边），路由 `ROUTES.DEVICE_COMMAND_POLICIES`。

`query_device_command`/`get_device_query_result` 只注册进 `build_root_tool_dispatcher`（`app/agent/tool_dispatch.py`），`root_tool_schemas()` 里加两条 schema；不进 `roles.py` 任何子角色的 `tools_allowlist`。`ROOT_OPS_SYSTEM_PROMPT`（`app/agent/chat_turn.py`）追加一段：说明可以对已配置凭据的资产发起只读命令查询，需要审批时要如实告知用户"已提交审批，等待结果"，不得编造输出。

## 9. 安全边界总结（对齐 `docs/AGENT_ARCHITECTURE.md` §9 的分级方式）

| 级别 | 本设计的落地 |
| :--- | :--- |
| L1 能力最小化 | 新增两个工具只挂根 Agent；子角色 `tools_allowlist` 不变 |
| L2 动作审查 | `command_name` 必须命中代码层目录；策略表只能"跳过审批"，不能扩充可执行的命令集合 |
| L3 风险分级 | 未分类命令强制 HITL；黑名单硬拒绝；白名单仅在资产非动态凭据时才免审批——动态凭据永远至少过一次人工 |
| L4 执行沙箱 | v1 只连接 SSH，命令模板固定、不接受自由参数拼接；输出截断，异常不透传原始文本 |
| L5 审计 | 复用 `HitlProposal`/`AuditLog` 的既有 append-only 记录；策略表本身的增删改也要 `log_audit`（管理员改白名单是安全敏感操作，需要留痕） |
| L6 预算 | 不新增预算维度；命令执行走的是根 Agent 的工具调用，仍受现有 loop/session 预算约束 |

## 10. 假设与开放问题

| 编号 | 内容 | 说明 |
| :--- | :--- | :--- |
| B1 | v1 命令目录只有 4 条只读命令，且 `ping` 不接受自由目标参数 | 先把通道和审批模型跑通；后续要加命令/要支持参数化，走代码 review 加目录条目，不改这次的架构 |
| B2 | 网络可达性是部署问题，不是本设计范围 | 后端进程要能实际连到 CMDB 里登记的设备网段，这是运行环境要求，不在这次的代码改动里 |
| B3 | `device_command_policies` 的增删改本身不需要审批 | 管理员账号自己把关（用户已确认），但必须有 `device_command_policy:manage` 权限 + 审计留痕 |
| B4 | 状态改变类命令（`command_type="state_changing"`）v1 目录暂不收录任何一条 | 用户已确认这类命令理论上可以被列入白名单，但实际收录哪些、怎么在管理页面上做风险提示，留给写实施计划时结合 UI 细化；这里先把数据模型和权限边界定下来 |

## 11. 涉及文件（为 writing-plans 准备）

| 文件 | 改动 |
| :--- | :--- |
| `backend/app/models/device_command_policy.py` | 新建 |
| `backend/alembic/versions/...` | 新建：`device_command_policies` 表 |
| `backend/app/crud/device_command_policy.py` | 新建：CRUD + 策略解析查询（含 §3.2 的优先级逻辑）+ 回收站方法 |
| `backend/app/agent/device_commands.py` | 新建：命令目录 |
| `backend/app/agent/executors.py` | 新增 `DeviceQueryExecutor` |
| `backend/app/agent/hitl.py` | `ActionType` 加 `device_query`；`_validated_payload` 加 `DeviceQueryPayload` 分支；`propose_action` 加策略解析与自动批准分支；`resume_proposal` 加 `dynamic_password` 参数；`ProposalSafeSummary` 加 `result_excerpt` |
| `backend/app/agent/hitl_tools.py` | 新增 `query_device_command`、`get_device_query_result` |
| `backend/app/agent/tool_dispatch.py` | 根调度器注册两个新工具 + schema |
| `backend/app/agent/chat_turn.py` | `ROOT_OPS_SYSTEM_PROMPT` 追加说明 |
| `backend/app/schemas/hitl.py` | `HitlDecideRequest` 加 `dynamic_credential_password` |
| `backend/app/schemas/device_command_policy.py` | 新建：Create/Update/Response |
| `backend/app/api/v1/hitl.py` | `decide_hitl_proposal` 校验动态凭据密码必填 |
| `backend/app/api/v1/device_command_policies.py` | 新建：策略管理 CRUD API |
| `backend/app/api/router.py` | 注册新路由 |
| `backend/app/core/config.py` | 新增 `DEVICE_COMMAND_TIMEOUT_SECONDS` |
| `backend/init_db.py` | `SEED_PERMISSIONS` 加两条权限码 |
| `backend/pyproject.toml` | `uv add asyncssh` |
| `frontend/src/types/device-command-policy.ts` | 新建 |
| `frontend/src/pages/DeviceCommandPoliciesPage.tsx` | 新建 |
| `frontend/src/components/ops-assistant/HitlApprovalCard.tsx` | 展示 `result_excerpt`；动态凭据资产的批准表单加密码输入框 |
| `frontend/src/lib/constants.ts` | 新增路由/权限常量 |
| `frontend/src/components/layout/Sidebar.tsx` | "运维管理"分组加菜单项 |
