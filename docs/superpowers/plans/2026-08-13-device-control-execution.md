# 设备变更类命令执行（device_control 真实通道） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `reboot`/`shutdown`/`port_enable`/`port_disable` 这 4 条变更类设备命令真正可执行：复用现有 `device_query` 的"命令目录 + 白/黑名单策略 + Scrapli 执行"流水线，白名单命中（且凭据非动态）当场自动执行，未分类走人工审批，黑名单直接拒绝；删除 `NotImplementedExecutor` 占位，`device_control` 不再永远停在 `PENDING`。

**Architecture:** 不新增数据模型、不新增 action_type、不新增状态机分支——把 `device_control` 的 payload 形状从写死的 `{command: Literal[4项]}` 改成跟 `device_query` 完全同构的 `{command_name, interface_name?}`，两者共用 `propose_action` 里同一段策略解析/自动批准逻辑与同一个执行器实例；模型侧新增一个专用只写工具 `propose_device_control`（镜像 `query_device_command` 的强类型写法），`propose_remediation` 收窄为只处理 `notify`。命令目录新增 `command_type="state_changing"` 的 4 条定义，`reboot`/`shutdown` 走 Scrapli `send_interactive` 处理确认提示，`port_enable`/`port_disable` 走 `send_configs` 配置模式并校验接口名参数。

**Tech Stack:** Python 3.14.3、FastAPI、SQLAlchemy 2 async、Pydantic v2、Scrapli（`AsyncIOSXEDriver`/`AsyncJunosDriver`/`AsyncHuaweiVRPDriver`/`AsyncGenericDriver`）、pytest、React 19、TypeScript、Zod、Vitest。

---

## Global Constraints

- 只在 `master` 分支工作；每个任务验证通过后按项目规范提交一次；commit message 禁止 `Co-Authored-By`。
- 后端命令一律 `cd backend` 后用 `uv run`；前端命令 `cd frontend` 后用 `pnpm`。
- **命令字符串仍然只能来自代码层目录**（`app/agent/device_commands.py`），策略表只决定"要不要跳审批"，永远不能决定执行内容——这是贯穿本计划最核心的不变量，任何一步都不能违反。
- **本计划做出的设计决策**（已与项目所有者确认，写在这里供实施时对照，不要在实施中重新讨论）：
  1. 首批收录全部 4 条变更类命令：`reboot`、`shutdown`、`port_enable`、`port_disable`。
  2. `device_control` 合并进 `device_query` 现有的"目录 + 策略"流水线，不维护第二套判断逻辑。
  3. 变更类命令（`command_type="state_changing"`）的白/黑名单策略**只能创建 `scope="asset"`**，不允许 `scope="asset_type"`——防止一次操作失误就对整个设备类型放行重启。
- **本计划新增的假设，需要在实施/验收时留意（不是设计确认项，是技术不确定性，写清楚以免被当成"肯定能跑"）：**
  - A1：网络设备（交换机/路由器）没有通用的"整机断电" CLI 语义；`shutdown` 命令目录**只登记 `linux`/`generic` 厂商**（真正的 `poweroff`）。网络厂商调用 `shutdown` 会走既有"该设备厂商不支持这个命令"fail-closed 路径，不会被误解成"重启"。
  - A2：`reboot`/`shutdown` 执行后设备可能在返回响应前就断开 SSH 连接（这是预期行为，不是故障）。v1 采用保守判定：只要 Scrapli 抛异常就记 `ok=False`，`message` 里提示"设备可能已断开连接，请人工核实是否已重启"，**不伪造"已确认执行成功"**——运维人员需要自行核实。这是"宁可少报成功，不可假报成功"的一贯风格，跟 `NotImplementedExecutor` 当初的设计哲学一致。
  - A3：Scrapli `send_interactive`/`send_configs` 对确认提示的正则匹配、Junos 配置是否需要显式 `commit`，均需要在真实或官方 mock transport 上验证（Task 4 的单元测试全部 mock 掉 Scrapli 连接，不依赖真实设备；但**首次对生产资产启用任何 `state_changing` 白名单策略前，必须先在测试网段的真实/模拟设备上手工验证一次**，这一步写进 Task 9 的验收清单，不能跳过）。
  - A4：`hp_comware` 映射到 `AsyncGenericDriver`（无 Scrapli 专用平台驱动），该驱动不支持 `send_configs` 配置模式，所以 `port_enable`/`port_disable` 不给 `hp_comware` 登记模板（跟 `ping` 早前对 `juniper_junos` 的处理是同一个"厂商未覆盖=不支持"模式，不是新规则）。
  - A5：`docs/superpowers/specs/2026-08-12-device-command-execution-design.md`、`docs/superpowers/plans/2026-08-12-cmdb-asset-credential-management.md` 等**归档设计/计划文档**仍会以现在时描述"`device_control` 执行器是 `NotImplementedExecutor` 占位"——这是有意保留的历史快照，不属于本计划的更新范围；本计划只更新仍在维护的 `docs/AGENT_ARCHITECTURE.md`。

---

## File Structure

| 文件                                                                                | 改动                                                                                                                                                                                                                                                                                                        |
| :---------------------------------------------------------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backend/app/agent/device_commands.py`                                              | 新增 4 条命令定义；`DeviceCommandDefinition` 加 `requires_argument`/`config_templates`/`confirmation` 字段；新增 `CommandConfirmation` dataclass、`command_type_of()`、接口名校验函数；**改写**（不是新增旁路函数）`command_supports_vendor()`/`list_commands_for_vendor()` 使其同时识别 `config_templates` |
| `backend/app/agent/hitl.py`                                                         | `DeviceQueryPayload`/`DeviceControlPayload` 合并为 `DeviceCommandPayload`；`_validated_payload` 改用统一模型；`propose_action` 策略解析分支扩展到 `device_control`；删除 `NotImplementedExecutor` 用法                                                                                                      |
| `backend/app/agent/executors.py`                                                    | `DeviceQueryExecutor.execute()` 扩展支持 `interface_name` 参数、确认交互、配置模式；删除 `NotImplementedExecutor`、`DeviceControlExecutor` Protocol                                                                                                                                                         |
| `backend/app/agent/hitl_tools.py`                                                   | 新增 `propose_device_control()`；`list_device_commands_for_asset()` 循环条件改用改写后的 `command_supports_vendor()`，让 port_enable/port_disable 能出现在发现工具的输出里                                                                                                                                  |
| `backend/app/agent/tool_dispatch.py`                                                | `ProposeRemediationArgs.action_type` 收窄为 `Literal["notify"]`；新增 `ProposeDeviceControlArgs` + schema + 调度分支                                                                                                                                                                                        |
| `backend/app/agent/chat_turn.py`                                                    | 系统提示词更新：说明 `propose_device_control` 与 `propose_remediation` 的边界                                                                                                                                                                                                                               |
| `backend/app/schemas/device_command_policy.py`                                      | `DeviceCommandPolicyCreate` 校验：`state_changing` 命令强制 `scope="asset"`                                                                                                                                                                                                                                 |
| `backend/tests/test_device_commands.py`                                             | 新增 4 条命令的目录测试、接口名校验测试                                                                                                                                                                                                                                                                     |
| `backend/tests/test_agent_hitl.py`                                                  | 重写/新增 `device_control` 相关用例（策略驱动而非"永远 PENDING"）                                                                                                                                                                                                                                           |
| `backend/tests/test_agent_executors.py`                                             | 删除 `NotImplementedExecutor` 测试；新增 `DeviceQueryExecutor` 状态变更命令的执行测试                                                                                                                                                                                                                       |
| `backend/tests/test_hitl_api.py`                                                    | 更新 `device_control` payload 形状与断言                                                                                                                                                                                                                                                                    |
| `backend/tests/test_hitl_integration.py`                                            | Scenario B 改走 `propose_device_control` 工具，payload 形状更新                                                                                                                                                                                                                                             |
| `backend/tests/test_chat_turn.py`                                                   | 更新 fixture 里的 `device_control` payload 形状                                                                                                                                                                                                                                                             |
| `backend/tests/test_agent_ws_hub.py`                                                | 更新 fixture 里的 `device_control` payload 形状                                                                                                                                                                                                                                                             |
| `backend/tests/test_agent_hitl_tools.py`                                            | 新增 `propose_device_control`/`ProposeDeviceControlArgs` 的 schema 与调度测试                                                                                                                                                                                                                               |
| `backend/tests/test_device_command_policy_schemas.py`（新建）                       | 新增 `DeviceCommandPolicyCreate` 的 `scope=asset_type` + `state_changing` 命令被拒绝的 schema 单测（仓库目前没有任何文件测这个 Pydantic schema，`test_device_command_policy_model.py` 只测 ORM 模型，不能塞进去）                                                                                           |
| `backend/tests/test_device_command_execution_integration.py`                        | 新增白名单自动执行 reboot、黑名单拒绝 port_disable、接口名非法被拒绝、`asset_type` 范围创建被拒绝等端到端用例                                                                                                                                                                                               |
| `frontend/src/types/device-command-policy.ts`                                       | `DEVICE_COMMAND_NAMES` 加 4 项；新增 `DEVICE_COMMAND_RISK` 映射                                                                                                                                                                                                                                             |
| `frontend/src/components/device-command-policies/DeviceCommandPolicyFormDialog.tsx` | 选中 `state_changing` 命令时强制 `scope="asset"` 并显示风险提示                                                                                                                                                                                                                                             |
| `docs/AGENT_ARCHITECTURE.md`                                                        | §4.2 工具契约表、§6/§9/A6 更新为"已接入"，不再声称 `device_control` 是 stub                                                                                                                                                                                                                                 |

---

### Task 1: 命令目录新增 4 条变更类命令

**Files:**
- Modify: `backend/app/agent/device_commands.py`
- Test: `backend/tests/test_device_commands.py`

**Interfaces:**
- Consumes: 无新依赖。
- Produces: `CommandName` 新增 4 个字面量；`CommandConfirmation` dataclass；`DeviceCommandDefinition.requires_argument`/`config_templates`/`confirmation`；`validate_interface_name(value: str) -> bool`。

- [ ] **Step 1: 写失败测试**

```python
def test_catalog_contains_state_changing_commands() -> None:
    names = {item.name for item in list_device_commands()}
    assert {"reboot", "shutdown", "port_enable", "port_disable"} <= names


def test_state_changing_commands_are_flagged() -> None:
    for name in ("reboot", "shutdown", "port_enable", "port_disable"):
        assert get_device_command(name).command_type == "state_changing"


def test_shutdown_only_supports_linux_generic() -> None:
    """网络设备没有通用整机关机语义，shutdown 只登记 linux/generic。"""
    shutdown = get_device_command("shutdown")
    assert set(shutdown.templates) == {"linux", "generic"}


def test_reboot_has_confirmation_for_network_vendors() -> None:
    reboot = get_device_command("reboot")
    assert reboot.confirmation is not None
    for vendor in ("cisco_iosxe", "huawei_vrp", "hp_comware", "juniper_junos"):
        assert vendor in reboot.confirmation


def test_port_commands_require_interface_argument() -> None:
    for name in ("port_enable", "port_disable"):
        assert get_device_command(name).requires_argument == "interface_name"
    for name in ("show_version", "reboot", "shutdown"):
        assert get_device_command(name).requires_argument == "none"


def test_port_commands_config_templates_exclude_generic_driver_vendors() -> None:
    """hp_comware/linux/generic 没有 Scrapli 配置模式驱动，不登记端口命令。"""
    port_disable = get_device_command("port_disable")
    assert port_disable.config_templates is not None
    assert set(port_disable.config_templates) == {"cisco_iosxe", "huawei_vrp", "juniper_junos"}
    assert "hp_comware" not in port_disable.config_templates
    assert "linux" not in port_disable.config_templates


def test_list_commands_for_vendor_includes_config_mode_only_commands() -> None:
    """port_enable/port_disable 的 templates={}，但通过 config_templates 支持——发现工具靠这个函数看到它们。"""
    names = {item.name for item in list_commands_for_vendor("cisco_iosxe")}
    assert {"port_enable", "port_disable", "reboot", "show_version"} <= names
    assert command_supports_vendor("port_disable", "cisco_iosxe") is True
    assert command_supports_vendor("port_disable", "hp_comware") is False


def test_junos_port_config_template_includes_explicit_commit() -> None:
    """Junos 是 set/delete + commit 模式，模板必须显式包含 commit。"""
    port_disable = get_device_command("port_disable")
    assert port_disable.config_templates is not None
    assert "commit" in port_disable.config_templates["juniper_junos"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("GigabitEthernet0/1", True),
        ("ge-0/0/1", True),
        ("Ethernet1/0/1", True),
        ("", False),
        ("eth0; rm -rf /", False),
        ("eth0\nreload", False),
        ("eth0 reload", False),
        ("a" * 65, False),
    ],
)
def test_interface_name_validation_is_strict_allowlist(value: str, expected: bool) -> None:
    assert validate_interface_name(value) is expected
```

- [ ] **Step 2: 运行确认失败**

```powershell
cd backend
uv run pytest tests/test_device_commands.py -v
```

Expected: `ImportError`/`AttributeError`（`CommandConfirmation`、`validate_interface_name`、新命令名均不存在）。

- [ ] **Step 3: 实现目录扩展**

```python
type CommandName = Literal[
    "show_version",
    "show_running_config",
    "show_interfaces",
    "ping",
    "reboot",
    "shutdown",
    "port_enable",
    "port_disable",
]
type RequiresArgument = Literal["none", "interface_name"]

DEVICE_COMMAND_CATALOG_VERSION = "t13-v1"

# 命令级正则、按厂商 CLI 语法书写；只用于 send_interactive 匹配确认提示，
# 不接受任何运行时输入，跟 templates 一样是代码层常量。
_INTERFACE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9/.\-]{1,64}$")


def validate_interface_name(value: str) -> bool:
    """接口名严格白名单校验：只允许字母数字/斜杠/点/短横线，拒绝空白与控制字符。"""
    return bool(_INTERFACE_NAME_PATTERN.fullmatch(value))


@dataclass(frozen=True, slots=True)
class CommandConfirmation:
    """交互式确认提示的匹配正则与应答内容（配 Scrapli send_interactive 使用）。"""

    prompt_pattern: str
    response: str


@dataclass(frozen=True, slots=True)
class DeviceCommandDefinition:
    """一条命令的完整定义：语义 + 按厂商区分的真实命令字符串。"""

    name: CommandName
    version: str
    description: str
    command_type: CommandType
    templates: Mapping[VendorName, str]
    requires_argument: RequiresArgument = "none"
    # 仅 config-mode 命令（如端口开关）使用；send_configs 而非 send_command 执行。
    config_templates: Mapping[VendorName, tuple[str, ...]] | None = None
    # 仅需要人工确认提示的 exec-mode 命令（reboot/shutdown）使用。
    confirmation: Mapping[VendorName, CommandConfirmation] | None = None
```

在 `_DEVICE_COMMAND_CATALOG` 字典里追加（`import re` 加到文件顶部）：

```python
    "reboot": DeviceCommandDefinition(
        name="reboot",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="重启设备（网络设备走 reload 语义）；执行前会等待设备确认提示",
        command_type="state_changing",
        templates={
            "generic": "sudo reboot",
            "linux": "sudo reboot",
            "cisco_iosxe": "reload",
            "huawei_vrp": "reboot",
            "hp_comware": "reboot",
            "juniper_junos": "request system reboot",
        },
        confirmation={
            "cisco_iosxe": CommandConfirmation(prompt_pattern=r"[Cc]onfirm", response="\n"),
            "huawei_vrp": CommandConfirmation(prompt_pattern=r"[Yy]/[Nn]", response="y"),
            "hp_comware": CommandConfirmation(prompt_pattern=r"[Yy]/[Nn]", response="y"),
            "juniper_junos": CommandConfirmation(prompt_pattern=r"yes,no", response="yes"),
        },
    ),
    "shutdown": DeviceCommandDefinition(
        name="shutdown",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description=(
            "关闭设备电源；仅 Linux/generic 主机有意义（网络设备没有通用整机断电 CLI，"
            "调用会按厂商不支持 fail-closed，不会被当成重启执行）"
        ),
        command_type="state_changing",
        templates={
            "generic": "sudo shutdown -h now",
            "linux": "sudo shutdown -h now",
        },
    ),
    "port_enable": DeviceCommandDefinition(
        name="port_enable",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="启用一个网络接口（no shutdown / undo shutdown 语义）",
        command_type="state_changing",
        templates={},
        requires_argument="interface_name",
        config_templates={
            "cisco_iosxe": ("interface {interface}", "no shutdown"),
            "huawei_vrp": ("interface {interface}", "undo shutdown"),
            "juniper_junos": ("delete interfaces {interface} disable", "commit"),
        },
    ),
    "port_disable": DeviceCommandDefinition(
        name="port_disable",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="禁用一个网络接口（shutdown 语义）",
        command_type="state_changing",
        templates={},
        requires_argument="interface_name",
        config_templates={
            "cisco_iosxe": ("interface {interface}", "shutdown"),
            "huawei_vrp": ("interface {interface}", "shutdown"),
            "juniper_junos": ("set interfaces {interface} disable", "commit"),
        },
    ),
```

`get_command_template()` 保持不变（只负责 exec 模式单行模板，`port_enable`/`port_disable` 的 `templates={}` 决定它对这两条命令永远抛 `UnsupportedVendorError`——这是有意的，因为这两条命令走 `config_templates` 专用路径，调用方不应该再调 `get_command_template` 取它们的字符串）。但 `command_supports_vendor()` 和 `list_commands_for_vendor()` 是"这个厂商能不能跑这条命令"的**权威判断**，会被 `hitl.py` 的策略校验、`hitl_tools.py` 的 `list_device_commands_for_asset()`（`list_device_commands` 工具的实现）共同复用来生成"该厂商支持的命令"提示——如果只看 `templates`，`port_enable`/`port_disable` 就会在这些提示和发现工具里永远消失，即使它们通过 `config_templates` 真实可执行。**必须直接改写这两个既有函数**，不要新增一个平行的 `vendor_supports_command`：

```python
def command_supports_vendor(command_name: str, vendor: str) -> bool:
    """命令名未知，或者两种登记方式（exec 模板 / config 模板）都没有这个厂商，才算不支持。"""
    definition = _DEVICE_COMMAND_CATALOG.get(command_name)  # type: ignore[arg-type]
    if definition is None:
        return False
    if vendor in definition.templates:
        return True
    return definition.config_templates is not None and vendor in definition.config_templates


def list_commands_for_vendor(vendor: str) -> tuple[DeviceCommandDefinition, ...]:
    """返回这个厂商能以任意方式（exec 或 config 模式）执行的全部命令定义。"""
    return tuple(
        definition for definition in _DEVICE_COMMAND_CATALOG.values()
        if command_supports_vendor(definition.name, vendor)
    )


def command_type_of(command_name: str) -> CommandType | None:
    """返回命令的风险分级；命令名未知时返回 None（调用方自行决定如何处理）。"""
    definition = _DEVICE_COMMAND_CATALOG.get(command_name)  # type: ignore[arg-type]
    return definition.command_type if definition else None
```

`hitl_tools.py::list_device_commands_for_asset()` 里原来的循环条件 `if asset.vendor not in definition.templates: continue` 要在 Task 4 里同步改成 `if not command_supports_vendor(definition.name, asset.vendor): continue`（否则这个已有的发现工具会一直看不到变更类命令）。

- [ ] **Step 4: 运行测试确认通过**

```powershell
uv run pytest tests/test_device_commands.py -v
uv run ruff check app/agent/device_commands.py tests/test_device_commands.py
uv run mypy app/agent/device_commands.py
```

- [ ] **Step 5: Commit**

```powershell
git add app/agent/device_commands.py tests/test_device_commands.py
git commit -m "命令目录新增 reboot/shutdown/port_enable/port_disable" -m "- 新增 4 条 state_changing 命令定义，复用现有目录的厂商模板机制
- reboot 按厂商登记确认提示正则与应答；shutdown 仅登记 linux/generic（网络设备没有通用整机断电语义）
- port_enable/port_disable 走独立 config_templates（config 模式多行命令），只覆盖有 Scrapli 专用驱动的厂商
- 新增严格接口名白名单校验 validate_interface_name，拒绝空白/控制字符/换行"
```

---

### Task 2: HITL payload 统一为 `DeviceCommandPayload`

**Files:**
- Modify: `backend/app/agent/hitl.py`
- Test: `backend/tests/test_agent_hitl.py`

**Interfaces:**
- Consumes: Task 1 的 `command_type_of`/`command_supports_vendor`/`validate_interface_name`。
- Produces: `DeviceCommandPayload`（取代 `DeviceQueryPayload` + `DeviceControlPayload`）。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.parametrize(
    ("action_type", "payload"),
    [
        ("unknown", {"message": "告警"}),
        ("notify", {"message": "告警", "secret": "不得接收"}),
        ("notify", {"message": 123}),
        ("device_control", {"command_name": 123}),
        ("device_control", {"command_name": "reboot", "interface_name": "eth0"}),  # reboot 不接受参数
        ("device_control", {"command_name": "port_disable"}),  # port_disable 缺 interface_name
    ],
)
async def test_propose_rejects_invalid_payload_before_insert(...):
    ...  # 已有测试参数化列表追加上面三行


async def test_device_control_rejects_read_only_command_name(
    db_session: AsyncSession, test_user: User
) -> None:
    """action_type=device_control 但传了只读命令名——两个工具的语义边界要在服务端强制。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)
    with pytest.raises(HitlProposalRejectedError, match="只读命令请使用 query_device_command"):
        await propose_action(
            db_session, session_id=session_id, proposed_by_agent_id=None,
            action_type="device_control", asset_id=asset_id,
            payload={"command_name": "show_version"}, reason="test",
            actor_user_id=test_user.id,
        )


async def test_device_query_rejects_state_changing_command_name(
    db_session: AsyncSession, test_user: User
) -> None:
    """反过来，query_device_command 也不能被用来偷跑变更类命令。"""
    session_id, asset_id = await _make_context(db_session, test_user.id)
    with pytest.raises(HitlProposalRejectedError, match="会改变设备状态的命令请使用 propose_device_control"):
        await propose_action(
            db_session, session_id=session_id, proposed_by_agent_id=None,
            action_type="device_query", asset_id=asset_id,
            payload={"command_name": "reboot"}, reason="test",
            actor_user_id=test_user.id,
        )
```

同时**删除**旧的 `test_device_control_never_auto_approves` 测试体（下个任务会补一个语义正确的替代测试：未分类命令仍然 PENDING，但理由变成"未分类"而不是"device_control 天生高风险"）。

- [ ] **Step 2: 运行确认失败**

```powershell
uv run pytest tests/test_agent_hitl.py -k "device_control or device_query" -v
```

- [ ] **Step 3: 实现**

删除 `DeviceQueryPayload`、`DeviceControlPayload`，替换为：

```python
class DeviceCommandPayload(BaseModel):
    """设备诊断/管控动作的严格载荷；两者共用同一形状。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    command_name: str = Field(min_length=1, max_length=100)
    interface_name: str | None = Field(default=None, min_length=1, max_length=64)
```

`_validated_payload` 里：

```python
        elif action_type in ("device_control", "device_query"):
            validated = DeviceCommandPayload.model_validate(candidate).model_dump()
```

`propose_action` 里原先只在 `if action_type == "device_query":` 分支做的资产/厂商/策略校验，改成对两种类型都跑，并新增命令类型交叉校验和接口名校验：

```python
    if action_type in ("device_query", "device_control"):
        command_name = stored_payload["command_name"]
        assert isinstance(command_name, str)

        command_type = command_type_of(command_name)
        if command_type is None:
            raise HitlProposalRejectedError(
                f"未知命令名：{command_name}；可用命令：{'、'.join(list_command_names())}"
            )
        if action_type == "device_query" and command_type != "read_only":
            raise HitlProposalRejectedError(
                "会改变设备状态的命令请使用 propose_device_control 工具，不能用 query_device_command"
            )
        if action_type == "device_control" and command_type != "state_changing":
            raise HitlProposalRejectedError(
                "只读命令请使用 query_device_command 工具，不需要走 propose_device_control"
            )

        if asset.credential_type == "none":
            raise HitlProposalRejectedError("该资产未配置登录凭据，无法执行设备命令")
        if not asset.vendor:
            raise HitlProposalRejectedError("资产未配置厂商信息，无法确定命令语法")
        if not command_supports_vendor(command_name, asset.vendor):
            supported = list_commands_for_vendor(asset.vendor)
            supported_hint = (
                f"该厂商支持的命令：{'、'.join(item.name for item in supported)}"
                if supported else "该厂商当前没有任何可用命令"
            )
            raise HitlProposalRejectedError(
                f"该设备厂商不支持这个命令（厂商 {asset.vendor}，命令 {command_name}）；{supported_hint}"
            )

        definition = get_device_command(command_name)
        interface_name = stored_payload.get("interface_name")
        if definition.requires_argument == "interface_name":
            if not isinstance(interface_name, str) or not validate_interface_name(interface_name):
                raise HitlProposalRejectedError("port_enable/port_disable 需要合法的接口名参数")
        elif interface_name is not None:
            raise HitlProposalRejectedError(f"命令 {command_name} 不接受 interface_name 参数")

        policy_decision = await device_command_policy_crud.resolve_policy(
            db, asset_id=asset.id, asset_type=asset.asset_type, command_name=command_name
        )
        if policy_decision == "blacklist":
            raise HitlProposalRejectedError("该命令已被列入黑名单，禁止执行")
    else:
        policy_decision = None
```

`_summary()`/`ProposalSafeSummary`、`HitlProposal.action_type not in (...)` 校验、`_publish` 均不需要改（`device_control` 早就是合法枚举值）。导入区加 `command_type_of, get_device_command, validate_interface_name, command_supports_vendor`。

- [ ] **Step 4: 运行测试确认通过**

```powershell
uv run pytest tests/test_agent_hitl.py -v
uv run ruff check app/agent/hitl.py tests/test_agent_hitl.py
uv run mypy app/agent/hitl.py
```

- [ ] **Step 5: Commit**

```powershell
git add app/agent/hitl.py tests/test_agent_hitl.py
git commit -m "device_control 合并进 device_query 的目录+策略校验流水线" -m "- DeviceQueryPayload/DeviceControlPayload 合并为 DeviceCommandPayload（command_name + 可选 interface_name）
- propose_action 的厂商/凭据/策略校验对 device_query 和 device_control 一视同仁
- 新增双向交叉校验：只读命令不能走 device_control，变更类命令不能走 device_query
- 新增 port_enable/port_disable 的 interface_name 必填+格式校验"
```

---

### Task 3: 自动批准与执行器分发统一；删除 stub

**Files:**
- Modify: `backend/app/agent/hitl.py`
- Modify: `backend/app/agent/executors.py`
- Test: `backend/tests/test_agent_hitl.py`
- Test: `backend/tests/test_agent_executors.py`
- Test: `backend/tests/test_hitl_api.py`

**Interfaces:**
- Consumes: Task 2 的策略解析结果。
- Produces: `DeviceQueryExecutor.execute()` 新增 `interface_name` 参数；`hitl.py` 不再 import `NotImplementedExecutor`。

- [ ] **Step 1: 写失败测试**

```python
# test_agent_hitl.py
async def test_unclassified_device_control_stays_pending(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未分类的变更类命令，即使 notify 自动批准打开，也必须停在 PENDING（跟 device_query 完全对称）。"""
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", True)
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    # 必须用 _make_query_asset（vendor+凭据齐全）；_make_context 的资产没有 vendor/凭据，
    # 会在凭据/厂商校验处先被拒绝，走不到"未分类停在 PENDING"这个断言。
    asset_id = await _make_query_asset(
        db_session, vendor="cisco_iosxe", credential_type="static",
        credential_password_encrypted="placeholder",
    )
    summary = await propose_action(
        db_session, session_id=session_id, proposed_by_agent_id=None,
        action_type="device_control", asset_id=asset_id,
        payload={"command_name": "reboot"}, reason="故障恢复",
        actor_user_id=test_user.id,
    )
    assert summary.status == "PENDING"


async def test_whitelisted_device_control_auto_executes_with_static_credential(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单 + 静态凭据：跟 device_query 一样一次调用直接 EXECUTED。"""
    ...  # 建 asset(vendor=cisco_iosxe, credential_type=static)，建 whitelist 策略，
        # monkeypatch app.agent.executors._open_scrapli_connection 返回假连接，
        # 断言 summary.status == "EXECUTED"


async def test_dynamic_credential_device_control_never_auto_executes(
    db_session: AsyncSession, test_user: User
) -> None:
    """跟 device_query 的既有规则完全对称：动态凭据永远至少过一次人工。"""
    ...  # asset credential_type=dynamic + whitelist 策略 → 仍 PENDING
```

**同时必须重写现有测试 `test_agent_hitl.py::test_device_control_stub_failure_stays_approved`（当前约第658行）**——它用 `_make_context`（无 vendor/凭据）+ `payload={"command": "shutdown"}` 断言"stub 失败保持 APPROVED"。Task 2 落地后这个测试会在 `propose_action` 校验阶段就因为字段名不对（`command` 而不是 `command_name`）和缺 vendor/凭据直接抛 `HitlProposalRejectedError`，测试原本想验证的"批准后真实执行失败仍保持 APPROVED"场景根本走不到。改造成：

```python
async def test_device_control_real_execution_failure_stays_approved(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未分类命令批准后，若真实设备连接失败，必须保持 APPROVED（不伪造 EXECUTED）。"""
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(
        db_session, vendor="linux", credential_type="static",
        credential_password_encrypted="placeholder",
    )
    proposal = await propose_action(
        db_session, session_id=session_id, proposed_by_agent_id=None,
        action_type="device_control", asset_id=asset_id,
        payload={"command_name": "shutdown"}, reason="维护窗口",
        actor_user_id=test_user.id,
    )
    assert proposal.status == "PENDING"
    await decide_proposal(
        db_session, proposal_id=proposal.proposal_id, approve=True,
        reviewed_by_user_id=test_user.id,
    )
    publisher = RecordingPublisher()

    with patch("app.agent.executors._open_scrapli_connection", side_effect=ConnectionError("unreachable")):
        summary = await resume_proposal(
            db_session, proposal_id=proposal.proposal_id,
            actor_user_id=test_user.id, publisher=publisher,
        )

    stored = await hitl_proposal_crud.get(db_session, proposal.proposal_id)
    assert stored is not None
    assert summary.status == "APPROVED"
    assert stored.executed_at is None
    assert [event[1] for event in publisher.events] == ["hitl_execution_failed"]
```

（这里选 `vendor="linux"` 而不是网络厂商——`shutdown` 按 Task 1 的设计只登记了 `linux`/`generic` 模板，网络厂商调用会先在"厂商不支持"处被拒绝，走不到真正的连接失败分支。）

```python
# test_agent_executors.py —— 删除 test_not_implemented_executor_always_fails，
# 新增（复用 test_device_query_executor.py 已有的 _make_asset/_generate_fernet_key 风格 helper）：

async def test_device_query_executor_reboot_sends_interactive_confirmation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reboot 命令要用 send_interactive 而不是 send_command。"""
    ...
    fake_connection = AsyncMock()
    fake_connection.send_interactive = AsyncMock(
        return_value=type("Resp", (), {"result": "System will reboot", "failed": False})()
    )
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        result = await executor.execute(db_session, asset=asset, command_name="reboot", dynamic_password=None)
    assert result.ok is True
    fake_connection.send_interactive.assert_awaited_once()
    fake_connection.send_command.assert_not_called()


async def test_device_query_executor_connection_drop_during_reboot_is_conservative_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2：连接在重启命令执行中断开，不得伪造成功，必须提示人工核实。"""
    fake_connection = AsyncMock()
    fake_connection.send_interactive = AsyncMock(side_effect=ConnectionError("closed"))
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        result = await executor.execute(db_session, asset=asset, command_name="reboot", dynamic_password=None)
    assert result.ok is False
    assert "人工核实" in result.message


async def test_device_query_executor_port_disable_uses_send_configs_with_interface(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """port_disable 走 send_configs，接口名要正确代入模板。"""
    fake_connection = AsyncMock()
    fake_response_item = type("R", (), {"result": "ok", "failed": False})()
    fake_connection.send_configs = AsyncMock(return_value=[fake_response_item, fake_response_item])
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        result = await executor.execute(
            db_session, asset=asset, command_name="port_disable",
            dynamic_password=None, interface_name="GigabitEthernet0/1",
        )
    assert result.ok is True
    sent_lines = fake_connection.send_configs.call_args.args[0]
    assert sent_lines == ["interface GigabitEthernet0/1", "shutdown"]


async def test_device_query_executor_rejects_invalid_interface_name_before_connecting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法接口名必须在建立设备连接之前就拒绝，不能把它当命令片段发出去。"""
    with patch("app.agent.executors._open_scrapli_connection") as mock_connect:
        result = await executor.execute(
            db_session, asset=asset, command_name="port_disable",
            dynamic_password=None, interface_name="eth0; reload",
        )
    assert result.ok is False
    mock_connect.assert_not_called()


async def test_device_query_executor_rejects_unsupported_vendor_before_connecting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """厂商不支持这条命令时，同样要在连接设备之前就失败（不是连接后才发现）。"""
    asset.vendor = "hp_comware"  # port_disable 的 config_templates 没有登记 hp_comware
    with patch("app.agent.executors._open_scrapli_connection") as mock_connect:
        result = await executor.execute(
            db_session, asset=asset, command_name="port_disable",
            dynamic_password=None, interface_name="GigabitEthernet0/1",
        )
    assert result.ok is False
    assert result.message == "该设备厂商不支持这个命令"
    mock_connect.assert_not_called()
```

- [ ] **Step 2: 运行确认失败**

```powershell
uv run pytest tests/test_agent_hitl.py tests/test_agent_executors.py -v
```

- [ ] **Step 3: 实现 —— `executors.py`**

删除 `DeviceControlExecutor` Protocol 和 `NotImplementedExecutor` 类（连同模块头部 docstring 里"device_control 预留 stub"的措辞一并更新为"device_control 现在走真实 Scrapli 执行"）。`DeviceQueryExecutor.execute` 签名加一个可选参数，内部按 `definition.config_templates`/`definition.confirmation` 分派：

```python
class DeviceQueryExecutor:
    """设备诊断/管控命令执行器：解析凭据、按厂商选真实命令、跑 Scrapli、截断输出。"""

    async def execute(
        self,
        db: AsyncSession,
        *,
        asset: CmdbAsset,
        command_name: str,
        dynamic_password: str | None,
        interface_name: str | None = None,
    ) -> ExecutionResult:
        # 凭据解析部分不变（static/dynamic/none 三分支照旧）……

        try:
            definition = get_device_command(command_name)
        except UnknownDeviceCommandError:
            return ExecutionResult(ok=False, message="未知命令名")

        if definition.requires_argument == "interface_name":
            if not interface_name or not validate_interface_name(interface_name):
                return ExecutionResult(ok=False, message="接口名参数无效")
        elif interface_name is not None:
            return ExecutionResult(ok=False, message="该命令不接受接口名参数")

        # 厂商支持校验也要在连接设备之前完成——跟接口名校验一样是纵深防御层，
        # 不依赖调用方（propose_action）已经提前挡过一次；旧实现把这一步放在
        # 连接之后，会导致对不支持的厂商也先真的建立一次连接才失败。
        if not command_supports_vendor(command_name, asset.vendor):
            return ExecutionResult(ok=False, message="该设备厂商不支持这个命令")

        connection = None
        try:
            connection = await _open_scrapli_connection(
                host=asset.ip_address, vendor=asset.vendor,
                username=asset.credential_username, password=password,
                timeout_seconds=settings.DEVICE_COMMAND_TIMEOUT_SECONDS,
            )

            if definition.config_templates is not None and asset.vendor in definition.config_templates:
                rendered = [
                    line.format(interface=interface_name) for line in definition.config_templates[asset.vendor]
                ]
                if any("<" in line or ">" in line for line in rendered):
                    return ExecutionResult(ok=False, message="命令模板含未解析占位符")
                responses = await connection.send_configs(rendered)
                failed = any(getattr(item, "failed", False) for item in responses)
                output = "\n".join(str(getattr(item, "result", "")) for item in responses)
            elif definition.confirmation is not None and asset.vendor in definition.confirmation:
                confirm = definition.confirmation[asset.vendor]
                template = definition.templates[asset.vendor]
                response = await connection.send_interactive(
                    [(template, confirm.prompt_pattern, False), (confirm.response, r".*", True)]
                )
                failed = getattr(response, "failed", False)
                output = str(getattr(response, "result", ""))
            else:
                # command_supports_vendor 已经确认过支持，这里只是取字符串，
                # 不会再抛 UnsupportedVendorError（两处判断依据是同一份目录数据）。
                template = get_command_template(command_name, asset.vendor)
                if "<" in template or ">" in template:
                    return ExecutionResult(ok=False, message="命令模板含未解析占位符")
                response = await connection.send_command(template)
                failed = getattr(response, "failed", False)
                output = str(getattr(response, "result", ""))
        except Exception:
            # A2：连接中断（尤其 reboot/shutdown 生效后）不得伪造成功，保守判失败。
            return ExecutionResult(
                ok=False,
                message="连接或执行命令失败；如果是重启/关机类命令，设备可能已经生效，请人工核实",
            )
        finally:
            if connection is not None:
                try:
                    await connection.close()
                except Exception:
                    pass

        if failed:
            return ExecutionResult(ok=False, message="设备返回命令执行失败")

        rendered_output, truncated = _truncate_output(output)
        return ExecutionResult(ok=True, message="命令执行完成", detail={"output": rendered_output, "truncated": truncated})
```

`import` 区从 `app.agent.device_commands` 额外导入 `get_device_command, validate_interface_name, command_supports_vendor`（删除不再需要的 `UnsupportedVendorError` 导入，因为上面这条路径不再依赖它抛异常）。

- [ ] **Step 4: 实现 —— `hitl.py` 自动批准 + resume_proposal**

```python
    operations = await get_effective_operations_config(db)
    if (action_type == "notify" and operations.hitl_notify_auto_approve) or (
        action_type in ("device_query", "device_control")
        and policy_decision == "whitelist"
        and asset.credential_type != "dynamic"
    ):
```

`resume_proposal` 里合并原来的两个分支：

```python
        elif proposal.action_type in ("device_query", "device_control"):
            raw_asset_id = proposal.action_payload.get("asset_id")
            asset_for_query = (
                await cmdb_asset_crud.get(db, raw_asset_id) if isinstance(raw_asset_id, int) else None
            )
            if asset_for_query is None:
                execution_result = ExecutionResult(ok=False, message="资产不存在")
            else:
                raw_command_name = proposal.action_payload.get("command_name")
                raw_interface_name = proposal.action_payload.get("interface_name")
                execution_result = await _DEVICE_QUERY_EXECUTOR.execute(
                    db, asset=asset_for_query, command_name=str(raw_command_name),
                    dynamic_password=dynamic_password,
                    interface_name=raw_interface_name if isinstance(raw_interface_name, str) else None,
                )
```

删除 `elif proposal.action_type == "device_control":` 旧分支和 `_DEVICE_CONTROL_EXECUTOR = NotImplementedExecutor()`，删除 `NotImplementedExecutor` 的 import。`if proposal.action_type == "device_query":` 那段"写回 `last_result_excerpt`"的逻辑改成 `if proposal.action_type in ("device_query", "device_control"):`。

- [ ] **Step 5: 运行测试确认通过**

```powershell
uv run pytest tests/test_agent_hitl.py tests/test_agent_executors.py tests/test_hitl_api.py tests/test_device_query_executor.py -v
uv run ruff check app/agent/hitl.py app/agent/executors.py
uv run mypy app/agent/hitl.py app/agent/executors.py
```

- [ ] **Step 6: 更新受影响的既有测试**

`test_hitl_api.py::test_approve_device_control_stays_approved_second_decide_conflicts` 依赖"stub 永远失败"的行为——改造成"未分类命令批准后没有真实设备可连（mock 连接失败），保持 APPROVED，二次审批仍 409"；payload 从 `{"command": "reboot"}` 改成 `{"command_name": "reboot"}`，并给测试资产补 `vendor`/`credential_type`（否则会在 `propose_action` 校验阶段就被拒绝，走不到"批准后执行失败"这个场景）。

`test_agent_hitl.py::test_device_control_stub_failure_stays_approved` 已在 Step 1 重写为 `test_device_control_real_execution_failure_stays_approved`，这里不重复。

- [ ] **Step 7: Commit**

```powershell
git add app/agent/hitl.py app/agent/executors.py tests/test_agent_hitl.py tests/test_agent_executors.py tests/test_hitl_api.py
git commit -m "device_control 接入真实 Scrapli 执行器，删除 NotImplementedExecutor" -m "- DeviceQueryExecutor 扩展支持 interface_name、send_interactive 确认交互、send_configs 配置模式
- 自动批准条件对 device_query/device_control 一视同仁：白名单+非动态凭据才自动执行
- resume_proposal 合并两个执行分支，删除 device_control 专属 stub 路径
- 连接中断（尤其 reboot/shutdown 生效后）保守判定为失败，不伪造已确认执行成功
- 更新受影响的既有测试：device_control 不再假设永远 PENDING/永远 stub 失败"
```

---

### Task 4: 新增 `propose_device_control` 根工具

**Files:**
- Modify: `backend/app/agent/hitl_tools.py`
- Modify: `backend/app/agent/tool_dispatch.py`
- Modify: `backend/app/agent/chat_turn.py`
- Test: `backend/tests/test_agent_hitl_tools.py`
- Test: `backend/tests/test_hitl_integration.py`
- Test: `backend/tests/test_chat_turn.py`
- Test: `backend/tests/test_agent_ws_hub.py`

**Interfaces:**
- Consumes: Task 2/3 的 `propose_action(action_type="device_control", ...)`。
- Produces: `hitl_tools.propose_device_control()`；`tool_dispatch.ProposeDeviceControlArgs`；根工具 schema 数量从 11 变为 12。

- [ ] **Step 1: 写失败测试**

```python
# test_agent_hitl_tools.py
async def test_propose_device_control_returns_pending_without_payload(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_propose_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        return ProposalSafeSummary(
            proposal_id=60, action_type="device_control", status="PENDING",
            reason="故障恢复", asset_id=9,
        )
    monkeypatch.setattr(hitl_tools, "propose_action", fake_propose_action)

    result = await hitl_tools.propose_device_control(
        db_session, session_id=1, actor_user_id=2, proposed_by_agent_id=None,
        asset_id=9, command_name="reboot", interface_name=None, reason="故障恢复",
    )
    assert result.control == "pending_approval"
    assert "60" in result.content


async def test_propose_device_control_returns_ok_with_output_when_auto_executed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_propose_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        return ProposalSafeSummary(
            proposal_id=61, action_type="device_control", status="EXECUTED",
            reason="故障恢复", asset_id=9, result_excerpt="fake reboot output",
        )
    monkeypatch.setattr(hitl_tools, "propose_action", fake_propose_action)

    result = await hitl_tools.propose_device_control(
        db_session, session_id=1, actor_user_id=2, proposed_by_agent_id=None,
        asset_id=9, command_name="reboot", interface_name=None, reason="故障恢复",
    )
    assert result.control == "ok"
    assert "fake reboot output" in result.content


def test_root_schema_adds_propose_device_control_and_narrows_propose_remediation() -> None:
    schemas = root_tool_schemas()
    functions = {item["function"]["name"]: item["function"] for item in schemas}
    assert len(functions) == 12

    remediation = functions["propose_remediation"]
    # 注意：Pydantic v2 对单值 Literal["notify"] 生成的是 "const"，不是 "enum"
    # （实测验证过；写成 enum 断言会直接 KeyError，不是断言失败）。
    assert remediation["parameters"]["properties"]["action_type"]["const"] == "notify"

    control = functions["propose_device_control"]
    control_params = control["parameters"]
    assert control_params["additionalProperties"] is False
    assert set(control_params["required"]) == {"asset_id", "command_name", "reason"}
    assert set(control_params["properties"]["command_name"]["enum"]) == {
        "show_version", "show_running_config", "show_interfaces", "ping",
        "reboot", "shutdown", "port_enable", "port_disable",
    }


async def test_root_dispatcher_routes_propose_device_control(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def fake_propose_device_control(db: AsyncSession, **kwargs: object) -> ToolResult:
        captured.update(kwargs)
        return ToolResult(control="pending_approval", content="提案 70 待审批")

    monkeypatch.setattr(tool_dispatch, "propose_device_control", fake_propose_device_control)
    dispatch = build_root_tool_dispatcher(db_session, session_id=21, actor_user_id=22)

    result = await dispatch(
        "propose_device_control",
        {"asset_id": 9, "command_name": "port_disable", "interface_name": "Gi0/1", "reason": "端口异常"},
    )
    assert result.control == "pending_approval"
    assert captured["command_name"] == "port_disable"
    assert captured["interface_name"] == "Gi0/1"
```

`test_hitl_integration.py::test_scenario_b_...` 改名/改内容：不再假设"device_control 无条件强制 HITL"，而是验证"**未分类**的 device_control 命令即使 notify 自动批准打开也强制 HITL"，工具调用从 `propose_remediation` 改成 `propose_device_control`，payload 从 `{"command": "reboot"}` 改成 `{"command_name": "reboot"}`（并确保测试资产有 `vendor`）。`test_chat_turn.py`/`test_agent_ws_hub.py` 里 fixture 的 `arguments='{"asset_id": 1, "action_type": "device_control", "payload": {}}'` 与直接 publish 的 `payload={"command": "reboot", ...}` 同步改成新形状（这两个测试只关心"敏感字段不出现在 WS 里"，改动是机械的字段改名，不改变测试意图）。

- [ ] **Step 2: 运行确认失败**

```powershell
uv run pytest tests/test_agent_hitl_tools.py tests/test_hitl_integration.py tests/test_chat_turn.py tests/test_agent_ws_hub.py -v
```

- [ ] **Step 3: 实现**

`hitl_tools.py` 新增（紧邻 `query_device_command` 之后）：

```python
async def propose_device_control(
    db: AsyncSession,
    *,
    session_id: int,
    actor_user_id: int,
    proposed_by_agent_id: str | None,
    asset_id: int,
    command_name: str,
    interface_name: str | None,
    reason: str,
    publisher: HitlEventPublisher | None = None,
) -> ToolResult:
    """对已配置凭据的资产发起会改变设备状态的命令（reboot/shutdown/port_enable/port_disable）。

    命中白名单且资产非动态凭据时当场执行；否则停在 pending_approval，
    需要人工审批（动态凭据资产还需要在批准时当场输入密码）。
    """
    payload: dict[str, object] = {"command_name": command_name}
    if interface_name is not None:
        payload["interface_name"] = interface_name

    try:
        summary = await propose_action(
            db, session_id=session_id, actor_user_id=actor_user_id,
            proposed_by_agent_id=proposed_by_agent_id, asset_id=asset_id,
            action_type="device_control", payload=payload, reason=reason,
            publisher=publisher,
        )
    except HitlProposalRejectedError as exc:
        return ToolResult(control="rejected", content=f"设备管控请求被拒绝：{exc}")
    except Exception as exc:
        return ToolResult(control="failed", content=f"设备管控请求创建失败：{type(exc).__name__}")

    if summary.status == "EXECUTED":
        output = summary.result_excerpt or "（无输出）"
        return ToolResult(control="ok", content=f"设备管控命令 {summary.proposal_id} 已自动批准并执行：\n{output}")
    if summary.status == "PENDING":
        return ToolResult(control="pending_approval", content=f"设备管控请求 {summary.proposal_id} 已创建，正在等待人工审批。")
    return ToolResult(control="failed", content=f"设备管控请求 {summary.proposal_id} 当前状态为 {summary.status}，未完成执行。")
```

`tool_dispatch.py`：

```python
class ProposeRemediationArgs(_Args):
    asset_id: int = Field(ge=1)
    action_type: Literal["notify"]
    payload: dict[str, object]
    reason: str = Field(min_length=1, max_length=2000)


class ProposeDeviceControlArgs(_Args):
    asset_id: int = Field(ge=1)
    command_name: CommandName
    interface_name: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)
```

`root_tool_schemas()` 里在 `query_device_command` schema 之后追加：

```python
    propose_control_parameters = deepcopy(ProposeDeviceControlArgs.model_json_schema())
    propose_control_parameters.pop("title", None)
    propose_control_parameters["properties"]["command_name"] = propose_control_parameters.pop("$defs")["CommandName"]
    ...
        {
            "type": "function",
            "function": {
                "name": "propose_device_control",
                "description": (
                    f"[{ROOT_TOOL_SCHEMA_VERSION}] 对已配置凭据的资产发起会改变设备状态的命令"
                    "（reboot/shutdown/port_enable/port_disable）。白名单命中且资产非动态凭据时"
                    "会当场执行，否则进入人工审批。port_enable/port_disable 必须提供 interface_name。"
                    "不确定这台设备支持哪些变更类命令时先调用 list_device_commands。"
                ),
                "parameters": propose_control_parameters,
            },
        },
```

`build_root_tool_dispatcher` 里新增一个 `if name == "propose_device_control":` 分支（写法完全镜像 `query_device_command` 分支），并把 `propose_remediation` 分支的调用参数保持不变（`action_type` 现在服务端 Pydantic 已经收窄为只接受 `"notify"`，无需额外代码改动）。

`chat_turn.py` 的 `ROOT_OPS_SYSTEM_PROMPT`——现有文案里有一句"需要整改或写操作时，只能调用 `propose_remediation` 提交提案，等待人工审批；"，这句话在本任务落地后是**错的**（`propose_remediation` 收窄为只处理 notify），必须**改写**而不是只追加新段落，否则系统提示词会同时存在两条互相矛盾的指令：

```text
需要发送站内通知时，调用 propose_remediation 提交提案；
需要对某台已在 CMDB 登记凭据的设备做会改变状态的操作（重启、关机、启用/禁用接口）时，
调用 propose_device_control（不要再用 propose_remediation 发起设备管控）；
port_enable/port_disable 必须提供 interface_name。
两者命中白名单都可能当场执行，否则进入人工审批，此时同样要如实告知用户
「已提交审批」，不得编造设备已重启/端口已切换/通知已发送。
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
uv run pytest tests/test_agent_hitl_tools.py tests/test_hitl_integration.py tests/test_chat_turn.py tests/test_agent_ws_hub.py -v
uv run ruff check app/agent/hitl_tools.py app/agent/tool_dispatch.py app/agent/chat_turn.py
uv run mypy app/agent/hitl_tools.py app/agent/tool_dispatch.py app/agent/chat_turn.py
```

- [ ] **Step 5: Commit**

```powershell
git add app/agent/hitl_tools.py app/agent/tool_dispatch.py app/agent/chat_turn.py tests/test_agent_hitl_tools.py tests/test_hitl_integration.py tests/test_chat_turn.py tests/test_agent_ws_hub.py
git commit -m "新增 propose_device_control 根工具，propose_remediation 收窄为仅 notify" -m "- 新增强类型工具 propose_device_control（镜像 query_device_command 写法），承载 4 条变更类命令
- ProposeRemediationArgs.action_type 收窄为 Literal[\"notify\"]，写操作与只读诊断分别有专用工具
- 系统提示词更新：说明何时用 propose_device_control、port 命令必须带 interface_name
- 同步更新依赖旧 device_control payload 形状的既有测试（chat_turn/ws_hub/hitl_integration）"
```

---

### Task 5: 策略层安全闸门——state_changing 命令强制 `scope=asset`

**Files:**
- Modify: `backend/app/schemas/device_command_policy.py`
- Test: `backend/tests/test_device_command_policy_schemas.py`（新建文件——仓库目前没有任何测试文件导入过 `DeviceCommandPolicyCreate`，`test_device_command_policy_model.py` 只测 ORM 模型本身，不能把 schema 单测塞进去）
- Test: `backend/tests/test_device_command_policy_api.py`

**Interfaces:**
- Consumes: Task 1 的 `command_type_of`。
- Produces: `DeviceCommandPolicyCreate` 校验规则扩展。

- [ ] **Step 1: 写失败测试**

```python
def test_state_changing_command_rejects_asset_type_scope() -> None:
    with pytest.raises(ValidationError, match="变更类命令.*scope.*asset"):
        DeviceCommandPolicyCreate(
            scope="asset_type", asset_type="switch",
            command_name="reboot", decision="whitelist",
        )


def test_state_changing_command_accepts_asset_scope() -> None:
    policy = DeviceCommandPolicyCreate(
        scope="asset", asset_id=1, command_name="reboot", decision="whitelist",
    )
    assert policy.command_name == "reboot"


def test_read_only_command_still_accepts_asset_type_scope() -> None:
    """回归：只读命令不受这条新规则影响。"""
    policy = DeviceCommandPolicyCreate(
        scope="asset_type", asset_type="switch",
        command_name="show_version", decision="whitelist",
    )
    assert policy.scope == "asset_type"
```

对应 API 层集成测试（`test_device_command_policy_api.py`，必须先用文件里已有的 `_grant_policy_permissions(db_session, test_user)` 授予 `device_command_policy:manage`，否则会先在权限依赖处返回 403 而不是走到 Pydantic 422；路由前缀是 `/api/v1/device-command-policies/policies`，不是 `/api/v1/device-command-policies`）：

```python
async def test_create_policy_rejects_asset_type_scope_for_state_changing_command(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: Headers,
) -> None:
    await _grant_policy_permissions(db_session, test_user)
    response = await client.post(
        "/api/v1/device-command-policies/policies",
        json={"scope": "asset_type", "asset_type": "switch", "command_name": "reboot", "decision": "whitelist"},
        headers=auth_headers,
    )
    assert response.status_code == 422
```

- [ ] **Step 2: 运行确认失败**

```powershell
uv run pytest tests/test_device_command_policy_schemas.py tests/test_device_command_policy_api.py -k "state_changing" -v
```

- [ ] **Step 3: 实现**

```python
from app.agent.device_commands import command_type_of, list_device_commands

...

    @model_validator(mode="after")
    def validate_scope_fields(self) -> Self:
        if self.scope == "asset_type":
            if not self.asset_type:
                raise ValueError("scope 为 asset_type 时必须填写 asset_type")
            if self.asset_id is not None:
                raise ValueError("scope 为 asset_type 时不能填写 asset_id")
            if command_type_of(self.command_name) == "state_changing":
                raise ValueError(
                    "变更类命令（reboot/shutdown/port_enable/port_disable）只能按单台设备（scope=asset）"
                    "配置白/黑名单，不允许按设备类型一次性放行"
                )
        else:
            if self.asset_id is None:
                raise ValueError("scope 为 asset 时必须填写 asset_id")
            if self.asset_type is not None:
                raise ValueError("scope 为 asset 时不能填写 asset_type")
        return self
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
uv run pytest tests/test_device_command_policy_schemas.py tests/test_device_command_policy_api.py -v
uv run ruff check app/schemas/device_command_policy.py tests/test_device_command_policy_schemas.py
uv run mypy app/schemas/device_command_policy.py
```

- [ ] **Step 5: Commit**

```powershell
git add app/schemas/device_command_policy.py tests/test_device_command_policy_schemas.py tests/test_device_command_policy_api.py
git commit -m "变更类设备命令的白/黑名单策略强制精确到单台资产" -m "- reboot/shutdown/port_enable/port_disable 只能 scope=asset 创建策略，拒绝 scope=asset_type
- 防止一次操作失误对整个设备类型放行重启/断电类命令
- 只读命令的 asset_type 级策略不受影响"
```

---

### Task 6: 端到端集成测试

**Files:**
- Test: `backend/tests/test_device_command_execution_integration.py`

**Interfaces:**
- Consumes: Task 1–5 全部改动。
- Produces: 无新生产代码，只加验收测试。

- [ ] **Step 1: 新增测试（写完直接跑，本任务不存在"先失败"阶段——前面任务已让生产代码就绪）**

```python
async def test_whitelisted_reboot_executes_with_interactive_confirmation(...):
    """白名单 + 静态凭据的交换机：propose_device_control 一次调用当场执行 reboot。"""
    # asset(vendor=cisco_iosxe) + whitelist(scope=asset, command_name=reboot)
    # monkeypatch _open_scrapli_connection 返回 mock send_interactive
    # dispatch("propose_device_control", {asset_id, command_name: "reboot", reason})
    # 断言 control == "ok"，密码不出现在返回内容里


async def test_blacklisted_port_disable_is_rejected_without_creating_proposal(...):
    ...


async def test_unclassified_port_enable_creates_pending_and_requires_interface_name(...):
    """未分类 port_enable：先验证缺 interface_name 时在 propose 阶段就拒绝，不建提案；
    补齐 interface_name 后走正常 PENDING → 人工批准 → 执行完成。"""
    ...


async def test_create_asset_type_scope_policy_for_reboot_is_rejected_via_api(...):
    ...


async def test_dynamic_credential_reboot_still_forces_manual_approval_even_when_whitelisted(...):
    ...
```

- [ ] **Step 2: 运行**

```powershell
uv run pytest tests/test_device_command_execution_integration.py -v
```

- [ ] **Step 3: Commit**

```powershell
git add tests/test_device_command_execution_integration.py
git commit -m "新增变更类设备命令端到端验收测试" -m "- 覆盖白名单自动执行 reboot、黑名单拒绝 port_disable、port_enable 缺接口名先拒绝
- 覆盖 asset_type 范围创建 reboot 策略被 API 拒绝、动态凭据强制人工审批两条安全线"
```

---

### Task 7: 前端——命令目录、风险提示、scope 限制

**Files:**
- Modify: `frontend/src/types/device-command-policy.ts`
- Modify: `frontend/src/components/device-command-policies/DeviceCommandPolicyFormDialog.tsx`

**Interfaces:**
- Consumes: 无后端新增端点（沿用已有 CRUD API，校验错误由后端 422 返回，前端负责提前禁用不合法选项 + 展示后端报错）。
- Produces: `DEVICE_COMMAND_RISK` 映射；表单里选中变更类命令时锁定 `scope=asset`。

- [ ] **Step 1: 更新类型与常量**

```typescript
export const DEVICE_COMMAND_NAMES = [
  "show_version",
  "show_running_config",
  "show_interfaces",
  "ping",
  "reboot",
  "shutdown",
  "port_enable",
  "port_disable",
] as const

/** 跟后端 app/agent/device_commands.py::command_type 手动保持一致，用于表单风险提示 */
export const STATE_CHANGING_COMMAND_NAMES = new Set([
  "reboot",
  "shutdown",
  "port_enable",
  "port_disable",
])

export function isStateChangingCommand(commandName: string): boolean {
  return STATE_CHANGING_COMMAND_NAMES.has(commandName)
}
```

- [ ] **Step 2: 表单联动**

在 `DeviceCommandPolicyFormDialog.tsx` 里，需要新增 `form.watch("command_name")`（现有代码只 `watch("scope")`，没有监听 `command_name`）；在一个 `useEffect` 里响应它的变化：

```typescript
const commandName = form.watch("command_name")

useEffect(() => {
  if (isStateChangingCommand(commandName)) {
    form.setValue("scope", "asset", { shouldValidate: true })
  }
}, [commandName, form])
```

若 `isStateChangingCommand(value)` 为真：
- 强制把 `scope` 设为 `"asset"`（上面的 `setValue`）并禁用 `scope` 选择器（不是仅仅提示，直接不给切换，因为后端也是硬拒绝）；
- 在 `command_name` 下拉的选项旁加一个醒目 `Badge`（如"变更类，需精确到设备"）；
- 表单顶部加一条 `Alert`（`variant="destructive"` 或警示色）："该命令会改变设备状态，仅能针对单台设备配置白/黑名单；请谨慎将常用变更类命令设为白名单。"

Zod schema（`command_name`/`scope` 联动校验）新增 `superRefine`：

```typescript
.superRefine((data, ctx) => {
  if (STATE_CHANGING_COMMAND_NAMES.has(data.command_name) && data.scope !== "asset") {
    ctx.addIssue({
      code: "custom",
      path: ["scope"],
      message: "变更类命令只能按单台设备配置",
    })
  }
})
```

- [ ] **Step 3: 运行**

```powershell
cd frontend
pnpm test
pnpm typecheck
pnpm lint
pnpm build
```

- [ ] **Step 4: Commit**

```powershell
git add src/types/device-command-policy.ts src/components/device-command-policies/DeviceCommandPolicyFormDialog.tsx
git commit -m "前端识别变更类设备命令并强制精确到单台资产" -m "- DEVICE_COMMAND_NAMES 补齐 4 条变更类命令，新增 STATE_CHANGING_COMMAND_NAMES 风险标记
- 选中变更类命令时表单锁定 scope=asset 并展示醒目警示，与后端硬校验保持一致体验
- Zod 表单规则同步拒绝 变更类命令 + scope=asset_type 的组合，提前拦截而不是等 422"
```

---

### Task 8: 全量回归验证并更新架构文档

**Files:**
- Verify: 全部改动
- Modify: `docs/AGENT_ARCHITECTURE.md`

- [ ] **Step 1: 更新文档，去掉"stub"措辞**

`docs/AGENT_ARCHITECTURE.md` §6 执行器表格、§9 L3/L4 行、§11 A6 行，把"当前唯一实现是 NotImplementedExecutor"改为"已接入：白名单+静态/无凭据当场执行，动态凭据强制人工审批，命令目录与 device_query 共用（见 docs/superpowers/plans/2026-08-13-device-control-execution.md）"。

另外，§4.2"写操作/提案类"工具表格目前只有一行 `propose_remediation | asset_id, action_type(notify\|device_control), payload, reason | ...`，本任务落地后这行描述已经错了（`action_type` 现在只有 `notify`），且表格里也没有 `query_device_command`/`propose_device_control`/`list_device_commands`/`get_device_query_result` 这四行。需要一并更新：把 `propose_remediation` 那行改成只写 `notify`，新增四行描述新工具的参数与返回。

- [ ] **Step 2: 全量验证**

```powershell
cd backend
uv run pytest -q
uv run ruff check .
uv run mypy app

cd ../frontend
pnpm test
pnpm typecheck
pnpm lint
pnpm build
```

Expected: 全部通过，无新增 lint 警告（超出既有基线）。

- [ ] **Step 3: 生产启用前的手工验证清单（对应 Global Constraints A3，不是自动化测试能覆盖的部分）**

- [ ] 在测试网段的真实或虚拟设备（至少覆盖 cisco_iosxe / huawei_vrp 各一台）上，用一个非生产资产手工触发一次 `reboot` 的白名单自动执行，确认 `send_interactive` 的确认提示正则命中、设备确实重启。
- [ ] 在同样的测试设备上手工验证一次 `port_disable`/`port_enable`，确认 `send_configs` 真的进入了配置模式并生效（尤其 Junos 的 `commit` 步骤）。
- [ ] 确认以上验证完成、且记录在案后，才允许对生产资产创建任何 `state_changing` 命令的白名单策略。

- [ ] **Step 4: Commit**

```powershell
git add docs/AGENT_ARCHITECTURE.md
git commit -m "更新架构文档：device_control 已接入真实执行通道" -m "- §6/§9/A6 去掉 NotImplementedExecutor stub 措辞，说明现在复用 device_query 的目录+策略+Scrapli 流水线
- 记录生产启用前需要在测试网段人工验证 send_interactive/send_configs 行为这一硬性前置条件"
```

---

## Acceptance Checklist

- [ ] `reboot`/`shutdown`/`port_enable`/`port_disable` 全部登记进命令目录，`shutdown` 只对 `linux`/`generic` 生效。
- [ ] `device_control` 走跟 `device_query` 完全对称的策略解析：黑名单硬拒绝且不建提案；白名单+非动态凭据当场执行；未分类走人工审批；动态凭据永远至少过一次人工。
- [ ] `query_device_command` 拒绝变更类命令名，`propose_device_control` 拒绝只读命令名，两个方向都要报可行动的错误原因。
- [ ] `port_enable`/`port_disable` 缺 `interface_name` 或格式非法时，在建立设备连接**之前**就拒绝。
- [ ] 变更类命令的白/黑名单策略只能 `scope=asset`，`asset_type` 范围创建返回 422（前端也提前拦截）。
- [ ] `NotImplementedExecutor`/`DeviceControlExecutor` 从代码库中移除，无遗留 import。
- [ ] 连接中断（尤其 reboot/shutdown 生效后）保守判定为失败并提示人工核实，绝不伪造已确认执行成功。
- [ ] 密码/密文/原始 payload 不出现在工具返回内容、WS 事件、日志或异常信息中。
- [ ] 后端全量 `pytest`/`ruff`/`mypy`、前端 `test`/`typecheck`/`lint`/`build` 全部通过。
- [ ] 生产启用前完成 Task 8 的手工验证清单。
