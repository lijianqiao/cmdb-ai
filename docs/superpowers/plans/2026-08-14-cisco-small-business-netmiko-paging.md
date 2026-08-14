# Cisco Small Business 交换机 Netmiko 分页修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Cisco SG350X 等 Small Business 交换机通过 Netmiko 官方 `cisco_s300` 驱动连接，正确清洗 ANSI 提示符并用 `terminal datadump` 关闭分页。

**Architecture:** CMDB 新增独立的 `cisco_small_business` CLI 平台值，后端命令目录为该平台登记经 Cisco 文档确认的命令，执行器只负责将它确定性映射到 Netmiko `cisco_s300`。ANSI、提示符和分页初始化继续由 Netmiko 官方驱动负责，不在业务层实现自定义 `--More--` 翻页循环。

**Tech Stack:** Python 3.14.3、FastAPI/Pydantic、Netmiko 4.7、pytest、React 19、TypeScript 6、Zod、Vitest。

## Global Constraints

- 只在当前 `master` 分支工作；不创建分支、worktree 或 PR。
- 所有后端 Python 命令都在 `backend/` 下用 `uv run` 执行；不直接调用系统 Python。
- 不新增或升级依赖；`netmiko>=4.7.0` 已存在。
- 新平台名固定为 `cisco_small_business`，Netmiko `device_type` 固定为 `cisco_s300`。
- `cisco_iosxe -> cisco_xe` 映射保持不变，不根据端口数量自动猜测平台。
- 不新增通用分页循环、SSH 自动探测或 Netmiko `session_log`。
- `cmdb_assets.vendor` 已是 `VARCHAR(50)`，不创建数据库迁移，也不批量改写现有资产。
- 自动化测试不得连接 `10.11.210.67` 或使用任何真实设备密码。
- 每个生产行为先写失败测试并确认失败原因，再写最小实现。
- 每次提交只包含当前任务的文件，提交信息使用中文且不包含 `Co-Authored-By`。

## File Structure

- `backend/app/agent/device_commands.py`：`VendorName` 权威定义，以及每个平台允许执行的确定性命令目录。
- `backend/app/agent/executors.py`：CMDB 平台值到 Netmiko `device_type` 的唯一映射和连接构造。
- `backend/tests/test_device_commands.py`：Small Business 命令目录与 fail-closed 行为测试。
- `backend/tests/test_cmdb_schemas.py`：CMDB 请求 schema 接受新平台值的测试。
- `backend/tests/test_netmiko_platforms.py`：新建；隔离验证驱动映射和 `ConnectHandler` 参数，不连接设备。
- `frontend/src/types/cmdb.ts`：前端 `VendorName` 联合类型。
- `frontend/src/components/cmdb/cmdbAssetFormSchema.ts`：表单 Zod 厂商枚举。
- `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx`：厂商下拉显示文本和已有值恢复。
- `frontend/src/components/cmdb/CmdbAssetFormDialog.test.tsx`：前端枚举和下拉配置测试。
- `docs/AGENT_ARCHITECTURE.md`：修正 A8，说明 `vendor` 是 CLI 平台及 SG350X 的官方驱动路径。
- `docs/superpowers/specs/2026-08-14-cisco-small-business-netmiko-paging-design.md`：实施完成后更新状态。

---

### Task 1: 后端 Small Business 平台与命令目录

**Files:**
- Modify: `backend/tests/test_cmdb_schemas.py`
- Modify: `backend/tests/test_device_commands.py`
- Modify: `backend/app/agent/device_commands.py:21-205`

**Interfaces:**
- Consumes: 现有 `VendorName`、`DeviceCommandDefinition`、`CommandConfirmation`、`get_command_template()`、`command_supports_vendor()`。
- Produces: `VendorName` 新值 `cisco_small_business`，以及该平台的只读、重启和端口启停命令定义，供 schema、执行器和前端使用。

- [ ] **Step 1: 写 CMDB schema 的失败测试**

在 `backend/tests/test_cmdb_schemas.py` 的厂商测试旁新增：

```python
def test_create_accepts_cisco_small_business_vendor() -> None:
    payload = CmdbAssetCreate.model_validate(
        _base_create_kwargs(vendor="cisco_small_business")
    )
    assert payload.vendor == "cisco_small_business"
```

- [ ] **Step 2: 运行 schema 测试并确认按预期失败**

Run（工作目录 `backend/`）：

```powershell
uv run pytest tests/test_cmdb_schemas.py::test_create_accepts_cisco_small_business_vendor -v
```

Expected: FAIL，Pydantic 报告 `cisco_small_business` 不属于当前 `VendorName`；失败发生在任何数据库或网络访问之前。

- [ ] **Step 3: 写命令目录的失败测试**

在 `backend/tests/test_device_commands.py` 新增：

```python
def test_cisco_small_business_uses_sg350x_commands() -> None:
    assert get_command_template("show_version", "cisco_small_business") == "show version"
    assert (
        get_command_template("show_running_config", "cisco_small_business")
        == "show running-config"
    )
    assert (
        get_command_template("show_interfaces", "cisco_small_business")
        == "show interfaces status"
    )
    assert get_command_template("ping", "cisco_small_business") == "ping ip 1.1.1.1"

    reboot = get_device_command("reboot")
    assert reboot.templates["cisco_small_business"] == "reload"
    assert reboot.confirmation is not None
    confirmation = reboot.confirmation["cisco_small_business"]
    assert confirmation.prompt_pattern == r"\([Yy]/[Nn]\)"
    assert confirmation.response == "y"

    port_enable = get_device_command("port_enable")
    assert port_enable.config_templates is not None
    assert port_enable.config_templates["cisco_small_business"] == (
        "interface {interface}",
        "no shutdown",
    )
    port_disable = get_device_command("port_disable")
    assert port_disable.config_templates is not None
    assert port_disable.config_templates["cisco_small_business"] == (
        "interface {interface}",
        "shutdown",
    )
    assert command_supports_vendor("shutdown", "cisco_small_business") is False
```

同时扩展两个已有断言：

```python
for vendor in (
    "cisco_iosxe",
    "cisco_small_business",
    "huawei_vrp",
    "hp_comware",
    "juniper_junos",
):
    assert vendor in reboot.confirmation
```

```python
assert set(port_disable.config_templates) == {
    "cisco_iosxe",
    "cisco_small_business",
    "huawei_vrp",
    "juniper_junos",
}
```

- [ ] **Step 4: 运行命令目录测试并确认按预期失败**

Run（工作目录 `backend/`）：

```powershell
uv run pytest tests/test_device_commands.py::test_cisco_small_business_uses_sg350x_commands -v
```

Expected: FAIL with `UnsupportedVendorError` 或 `KeyError: 'cisco_small_business'`，证明测试捕获的是缺少平台定义，而不是测试语法错误。

- [ ] **Step 5: 最小实现新平台和经验证的命令**

在 `backend/app/agent/device_commands.py` 的 `VendorName` 中加入：

```python
type VendorName = Literal[
    "cisco_iosxe",
    "cisco_small_business",
    "huawei_vrp",
    "hp_comware",
    "juniper_junos",
    "linux",
    "generic",
]
```

在对应命令映射中加入下列条目，现有平台条目保持原样：

```python
# show_version.templates
"cisco_small_business": "show version",

# show_running_config.templates
"cisco_small_business": "show running-config",

# show_interfaces.templates
"cisco_small_business": "show interfaces status",

# ping.templates
"cisco_small_business": "ping ip 1.1.1.1",

# reboot.templates
"cisco_small_business": "reload",

# reboot.confirmation
"cisco_small_business": CommandConfirmation(
    prompt_pattern=r"\([Yy]/[Nn]\)",
    response="y",
),

# port_enable.config_templates
"cisco_small_business": ("interface {interface}", "no shutdown"),

# port_disable.config_templates
"cisco_small_business": ("interface {interface}", "shutdown"),
```

不要给整机 `shutdown` 增加 Small Business 模板。

- [ ] **Step 6: 运行定向测试并确认通过**

Run（工作目录 `backend/`）：

```powershell
uv run pytest tests/test_cmdb_schemas.py tests/test_device_commands.py -v
```

Expected: PASS；既有无效厂商、Linux 不支持运行配置、网络设备不支持整机关机等 fail-closed 测试继续通过。

- [ ] **Step 7: 提交后端平台契约**

```powershell
git add -- backend/app/agent/device_commands.py backend/tests/test_cmdb_schemas.py backend/tests/test_device_commands.py
git commit -m "增加 Cisco Small Business 设备平台" -m "- 让 CMDB schema 接受独立的 cisco_small_business CLI 平台，避免与 IOS-XE 混用`n- 为 SG350X 登记已核对的查询、重启和端口启停命令，并保持整机关机失败关闭`n- 补充平台命令和请求校验测试，防止后续厂商枚举再次漂移"
```

---

### Task 2: Netmiko 官方 `cisco_s300` 驱动映射

**Files:**
- Create: `backend/tests/test_netmiko_platforms.py`
- Modify: `backend/app/agent/executors.py:123-160`

**Interfaces:**
- Consumes: Task 1 产生的 `VendorName="cisco_small_business"`。
- Produces: `_netmiko_device_type_for_vendor("cisco_small_business") -> "cisco_s300"`；`_open_netmiko_connection()` 把该值传给 `ConnectHandler`。

- [ ] **Step 1: 写驱动映射和连接构造的失败测试**

新建 `backend/tests/test_netmiko_platforms.py`：

```python
"""CMDB CLI 平台到 Netmiko 官方驱动的映射测试；不建立真实网络连接。"""

from unittest.mock import MagicMock, patch

from app.agent.executors import (
    _netmiko_device_type_for_vendor,
    _open_netmiko_connection,
)


def test_cisco_platforms_use_distinct_netmiko_drivers() -> None:
    assert _netmiko_device_type_for_vendor("cisco_iosxe") == "cisco_xe"
    assert _netmiko_device_type_for_vendor("cisco_small_business") == "cisco_s300"


def test_open_small_business_connection_uses_cisco_s300_driver() -> None:
    connection = MagicMock()
    with patch("app.agent.executors.ConnectHandler", return_value=connection) as connect:
        result = _open_netmiko_connection(
            host="10.0.0.67",
            vendor="cisco_small_business",
            username="admin",
            password="test-only",
            conn_timeout=11.0,
        )

    assert result is connection
    connect.assert_called_once_with(
        device_type="cisco_s300",
        host="10.0.0.67",
        username="admin",
        password="test-only",
        conn_timeout=11.0,
        auth_timeout=11.0,
        banner_timeout=11.0,
    )
```

- [ ] **Step 2: 运行测试并确认按预期失败**

Run（工作目录 `backend/`）：

```powershell
uv run pytest tests/test_netmiko_platforms.py -v
```

Expected: 两个 Small Business 断言 FAIL，实际值为当前 fallback 的 `generic`；测试中的 `ConnectHandler` 已被 patch，不会连接网络。

- [ ] **Step 3: 添加唯一的驱动映射**

在 `backend/app/agent/executors.py` 的 `_NETMIKO_DEVICE_TYPES` 中加入：

```python
_NETMIKO_DEVICE_TYPES: Mapping[str, str] = {
    "cisco_iosxe": "cisco_xe",
    "cisco_small_business": "cisco_s300",
    "huawei_vrp": "huawei_vrp",
    "hp_comware": "hp_comware",
    "juniper_junos": "juniper_junos",
    "linux": "linux",
    "generic": "generic",
}
```

更新映射旁的中文注释，明确 `cisco_s300` 驱动会开启 ANSI 清洗并发送 `terminal datadump`，而 `cisco_xe` 使用 IOS-XE 会话初始化。不要修改 `_open_netmiko_connection()` 的其它参数，也不要增加显式 `expect_string` 或分页循环。

- [ ] **Step 4: 运行驱动与执行器定向测试并确认通过**

Run（工作目录 `backend/`）：

```powershell
uv run pytest tests/test_netmiko_platforms.py tests/test_device_query_executor.py tests/test_agent_executors.py -v
```

Expected: PASS；现有静态/动态凭据、超时传递、`dispatched`、确认命令和配置命令测试不回归。

- [ ] **Step 5: 离线确认 Netmiko 4.7 的官方类分派**

Run（工作目录 `backend/`；`auto_connect=False`，不会访问网络）：

```powershell
@'
from netmiko import ConnectHandler

connection = ConnectHandler(
    device_type="cisco_s300",
    host="offline.invalid",
    auto_connect=False,
)
print(type(connection).__module__, type(connection).__name__)
'@ | uv run python -
```

Expected:

```text
netmiko.cisco.cisco_s300 CiscoS300SSH
```

- [ ] **Step 6: 提交驱动映射**

```powershell
git add -- backend/app/agent/executors.py backend/tests/test_netmiko_platforms.py
git commit -m "为 SG350X 选择 Netmiko 官方驱动" -m "- 将 cisco_small_business 确定性映射到 cisco_s300，让驱动使用 ANSI 清洗和 terminal datadump`n- 保留 IOS-XE 到 cisco_xe 的原映射，避免不同 Cisco CLI 平台互相影响`n- 用无网络连接的单元测试锁定 ConnectHandler 参数和驱动选择"
```

---

### Task 3: 前端 CMDB 平台选择

**Files:**
- Modify: `frontend/src/components/cmdb/CmdbAssetFormDialog.test.tsx`
- Modify: `frontend/src/types/cmdb.ts:5-13`
- Modify: `frontend/src/components/cmdb/cmdbAssetFormSchema.ts:7-15`
- Modify: `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx:75-82,169-181`

**Interfaces:**
- Consumes: Task 1 定义的精确字符串 `cisco_small_business`。
- Produces: 前端 `VendorName`、Zod schema、下拉选项和旧值恢复逻辑都能处理该平台；UI 文案固定为“思科 Small Business（SG350X 等）”。

- [ ] **Step 1: 写 Zod 枚举的失败测试**

在 `frontend/src/components/cmdb/CmdbAssetFormDialog.test.tsx` 新增：

```typescript
it("允许选择 Cisco Small Business CLI 平台", () => {
  const result = createFormSchema(null).safeParse({
    ...baseAssetFields,
    vendor: "cisco_small_business",
    credential_type: "none",
    ...clearedCredentialFields(),
  })
  expect(result.success).toBe(true)
})
```

- [ ] **Step 2: 运行测试并确认按预期失败**

Run（工作目录 `frontend/`）：

```powershell
npm test -- src/components/cmdb/CmdbAssetFormDialog.test.tsx
```

Expected: FAIL，`result.success` 为 `false`，Zod 报告厂商值不属于当前枚举。

- [ ] **Step 3: 最小扩展 TypeScript 与 Zod 枚举**

在 `frontend/src/types/cmdb.ts` 的 `VendorName` 加入：

```typescript
export type VendorName =
  | "cisco_iosxe"
  | "cisco_small_business"
  | "huawei_vrp"
  | "hp_comware"
  | "juniper_junos"
  | "linux"
  | "generic"
```

在 `frontend/src/components/cmdb/cmdbAssetFormSchema.ts` 的 `VENDOR_VALUES` 加入：

```typescript
const VENDOR_VALUES = [
  "cisco_iosxe",
  "cisco_small_business",
  "huawei_vrp",
  "hp_comware",
  "juniper_junos",
  "linux",
  "generic",
] as const satisfies readonly VendorName[]
```

- [ ] **Step 4: 运行 Zod 测试并确认通过**

Run（工作目录 `frontend/`）：

```powershell
npm test -- src/components/cmdb/CmdbAssetFormDialog.test.tsx
```

Expected: PASS，包括所有原有凭据三态规则。

- [ ] **Step 5: 在绿灯状态下导出已有厂商配置**

把 `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx` 中已有的常量从：

```typescript
const VENDOR_ITEMS: { label: string; value: VendorName }[] = [
```

改为：

```typescript
export const VENDOR_ITEMS: { label: string; value: VendorName }[] = [
```

此步骤只暴露现有只读配置用于测试，不增加新平台行为。随后运行：

```powershell
npm test -- src/components/cmdb/CmdbAssetFormDialog.test.tsx
npm run typecheck
```

Expected: 仍然 PASS，确认重构没有改变表单行为。

- [ ] **Step 6: 写厂商下拉配置的失败测试**

将 `VENDOR_ITEMS` 从 `CmdbAssetFormDialog.tsx` 导入测试文件：

```typescript
import { VENDOR_ITEMS } from "./CmdbAssetFormDialog"
```

新增：

```typescript
it("显示 SG350X 对应的 Cisco Small Business 厂商选项", () => {
  expect(VENDOR_ITEMS).toContainEqual({
    label: "思科 Small Business（SG350X 等）",
    value: "cisco_small_business",
  })
})
```

- [ ] **Step 7: 运行测试并确认缺少下拉配置**

Run（工作目录 `frontend/`）：

```powershell
npm test -- src/components/cmdb/CmdbAssetFormDialog.test.tsx
```

Expected: FAIL；`VENDOR_ITEMS` 已能正常导入，但数组中缺少期望对象，证明测试捕获的是下拉选项尚未登记。

- [ ] **Step 8: 添加下拉项和已有值恢复**

在 `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx` 导出已有常量并加入新选项：

```typescript
export const VENDOR_ITEMS: { label: string; value: VendorName }[] = [
  { label: "通用", value: "generic" },
  { label: "思科 IOS-XE", value: "cisco_iosxe" },
  {
    label: "思科 Small Business（SG350X 等）",
    value: "cisco_small_business",
  },
  { label: "华为 VRP", value: "huawei_vrp" },
  { label: "H3C Comware", value: "hp_comware" },
  { label: "Juniper Junos", value: "juniper_junos" },
  { label: "Linux", value: "linux" },
]
```

同时在 `resolveVendor()` 的 `known` 列表中加入：

```typescript
"cisco_small_business",
```

- [ ] **Step 9: 运行前端定向测试、类型检查和 lint**

Run（工作目录 `frontend/`）：

```powershell
npm test -- src/components/cmdb/CmdbAssetFormDialog.test.tsx
npm run typecheck
npm run lint
```

Expected: 全部 PASS；TypeScript 确认前端三处枚举保持一致。

- [ ] **Step 10: 提交前端平台选项**

```powershell
git add -- frontend/src/types/cmdb.ts frontend/src/components/cmdb/cmdbAssetFormSchema.ts frontend/src/components/cmdb/CmdbAssetFormDialog.tsx frontend/src/components/cmdb/CmdbAssetFormDialog.test.tsx
git commit -m "在 CMDB 中增加 Small Business 平台选项" -m "- 同步 VendorName、Zod 枚举和已有值恢复逻辑，允许保存 cisco_small_business`n- 在厂商下拉中明确标注 SG350X 对应的 Cisco Small Business CLI 平台`n- 补充表单校验和选项文案测试，防止前后端枚举再次漂移"
```

---

### Task 4: 架构文档、全量回归与交付说明

**Files:**
- Modify: `docs/AGENT_ARCHITECTURE.md:516-518`
- Modify: `docs/superpowers/specs/2026-08-14-cisco-small-business-netmiko-paging-design.md:1-4`

**Interfaces:**
- Consumes: Tasks 1-3 已通过测试的平台值、命令目录、驱动映射和前端选项。
- Produces: 与代码一致的运维约束、完整验证证据，以及用户可执行的 CMDB 修正与只读实机验收步骤。

- [ ] **Step 1: 更新架构假设 A8**

把 `docs/AGENT_ARCHITECTURE.md` 的 A8 改为明确的 CLI 平台语义：

```markdown
| A8 | CMDB `vendor` 字段必须与设备实际 CLI 平台一致 | Netmiko 按 `device_type` 决定 ANSI、提示符和分页初始化。Cisco IOS-XE 使用 `cisco_xe` + `terminal length 0`；Cisco Small Business（SG350X 等）使用 `cisco_s300` + `terminal datadump`，且该驱动会开启 ANSI 清洗。平台标错会导致 `ESC[K` 污染提示符或大输出停在分页提示符，增加超时不能修复。 |
```

把设计文档状态从“待用户书面审查”改为：

```text
状态：已批准并实施
```

- [ ] **Step 2: 运行后端完整质量门禁**

Run（工作目录 `backend/`）：

```powershell
uv run ruff check app tests
uv run mypy app
uv run pytest
```

Expected: 三条命令全部 exit code 0；pytest 不建立真实设备连接。

- [ ] **Step 3: 运行前端完整质量门禁**

Run（工作目录 `frontend/`）：

```powershell
npm run lint
npm run typecheck
npm test
npm run build
```

Expected: 四条命令全部 exit code 0；构建产物成功生成。

- [ ] **Step 4: 检查差异只包含本计划范围**

Run（仓库根目录）：

```powershell
git status --short
git diff --check
git diff --stat HEAD~3
```

Expected: 没有空白错误；变更只涉及本计划列出的后端、前端、测试和文档文件，不包含真实密码、`.env` 或 Netmiko session log。

- [ ] **Step 5: 提交文档与验证结论**

```powershell
git add -- docs/AGENT_ARCHITECTURE.md docs/superpowers/specs/2026-08-14-cisco-small-business-netmiko-paging-design.md
git commit -m "记录 SG350X 官方驱动与验收约束" -m "- 明确 CMDB vendor 表达 CLI 平台，区分 IOS-XE 与 Cisco Small Business`n- 记录 cisco_s300 的 ANSI 清洗和 terminal datadump 分页初始化行为`n- 标记设计已实施，并保留真实设备只读验收需单独确认的边界"
```

- [ ] **Step 6: 向用户交付手工操作说明，不连接设备**

交付信息必须明确：

```text
1. 在 CMDB 编辑 10.11.210.67。
2. 把厂商从“思科 IOS-XE”改为“思科 Small Business（SG350X 等）”。
3. 保持凭据类型为“静态密码”；只改厂商不会清除已有密文。
4. 保存后，再让运维助手查询设备配置。
5. 如需由 Codex 直接进行只读实机验收，必须另行明确授权。
```

最终报告列出：各任务提交、定向测试、完整测试、未执行真实设备连接，以及没有 push。
