# 设备命令执行能力 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让运维助手能对 CMDB 里配置了登录凭据的资产发起只读诊断命令查询——白名单命中直接执行并把结果讲给用户，未分类走现有 HITL 人工审批（审批卡片就在聊天消息流里），黑名单硬拒绝；动态凭据资产无论策略如何都强制人工在场输入密码。

**Architecture:** 完全复用现有 `HitlProposal` 状态机（`propose_action`/`decide_proposal`/`resume_proposal`），`action_type` 加第三种 `device_query`；新增一张数据库表 `device_command_policies` 做白/黑名单（系统管理页面可配），但真正会在设备上执行的命令字符串永远来自代码层目录 `app/agent/device_commands.py`——策略只决定"要不要跳过审批"，不能凭空发明命令。连接层用 Scrapli（原生 async，多厂商命令行交互已经是它解决过的问题），凭据复用 `CmdbAsset` 已有的加密字段 + 新增的 `vendor` 字段。

**Tech Stack:** FastAPI + SQLAlchemy 2 async + PostgreSQL + Alembic + Pydantic 2（后端）；React 19 + TypeScript + react-hook-form + zod（前端）；新增依赖 `scrapli` + `scrapli-community`（多厂商 SSH 命令行交互）。

**Spec 参考：** `docs/superpowers/specs/2026-08-12-device-command-execution-design.md`（已经用户确认，本计划的每个决定都能在那份文档里找到依据，实施时如果发现代码现状跟 spec 描述不一致，以读到的真实代码为准，但设计意图仍以 spec 为准）。

## Global Constraints

- Python `>=3.14,<3.15`；后端命令一律 `uv run <cmd>`（从 `backend/`），前端命令一律 `npm run <script>`（从 `frontend/`）。
- 直接在 `master` 提交，不开分支；每个任务一个中文 commit（标题 + 空行 + 要点），不写 `Co-Authored-By`。
- TDD：先写失败测试，确认失败原因正确，再写最小实现，再确认通过。
- **明文密码（无论是解密出的静态密码还是审批时输入的动态密码）永远不进任何响应体、日志、审计 detail 字段、`HitlProposal.action_payload`。** 这是硬性安全约束，延续 CMDB 凭据管理那次的规矩，每个任务的测试都要覆盖到。
- **`VendorName` 这个 Literal 定义在 `app/agent/device_commands.py`（命令目录），不在 `app/schemas/cmdb.py` 里单独定义一份。** `app/schemas/cmdb.py` 从 `app.agent.device_commands` 导入复用。这跟项目里 `CredentialType` 自成一派定义在 schemas 层的既有写法不一样，是刻意的：厂商是否有效，唯一权威来源就是命令目录（目录里没有这个厂商的任何命令模板，这个厂商值本身就没有意义），两处各写一份容易走漂移。任务顺序上，先建目录（Task 1）再给 `CmdbAsset` 加 `vendor` 字段（Task 2），就是为了让这个依赖方向天然成立。
- 策略表（`device_command_policies`）只能决定"要不要跳过审批"，绝不能让命令字符串本身来自数据库或用户输入拼接——这是整个设计最核心的安全边界，每个任务如果涉及"命令内容从哪来"，答案永远是"`app/agent/device_commands.py` 这个代码层目录"。
- 全量验证命令（每个后端任务收尾都要跑，最后一个任务再跑一次全量）：`uv run pytest -v`、`uv run mypy app`、`uv run ruff check .`、`uv run alembic heads`（预期唯一 head）。前端：`npm run typecheck`、`npm run lint`、`npm run test`。
- Scrapli 相关测试（Task 6）不接真实设备——用 Scrapli 自带的 mock/fake transport 或者对 `DeviceQueryExecutor` 内部拆出的"取模板 + 建连接"两步分别打桩测试，不需要跑真实网络 I/O 就能验证分支逻辑正确。

## File Structure

| File | Responsibility |
| :--- | :--- |
| `backend/app/agent/device_commands.py` | 命令目录：`VendorName`/`CommandName`/`CommandType`/`DeviceCommandDefinition`，只读、代码层、版本化。 |
| `backend/app/models/cmdb_asset.py` | 加 `vendor` 字段。 |
| `backend/app/models/device_command_policy.py` | 新建：白/黑名单策略表模型。 |
| `backend/alembic/versions/...` | 两个新迁移：`cmdb_assets.vendor` 列；`device_command_policies` 表。 |
| `backend/app/crud/device_command_policy.py` | 新建：CRUD + `resolve_policy` 优先级解析 + 回收站方法。 |
| `backend/app/schemas/cmdb.py` | `CmdbAssetCreate`/`Update`/`Response` 加 `vendor: VendorName`。 |
| `backend/app/schemas/device_command_policy.py` | 新建：Create/Update/Response。 |
| `backend/app/api/v1/cmdb.py` | 创建/更新资产时透传 `vendor`。 |
| `backend/app/api/v1/device_command_policies.py` | 新建：策略管理 CRUD API。 |
| `backend/app/agent/hitl.py` | `ActionType` 加 `device_query`；`DeviceQueryPayload`；`propose_action` 加策略解析与自动批准分支；`resume_proposal` 加 `dynamic_password`；`ProposalSafeSummary` 加 `result_excerpt`。 |
| `backend/app/agent/executors.py` | 新增 `DeviceQueryExecutor`（Scrapli 连接）。 |
| `backend/app/agent/hitl_tools.py` | 新增 `query_device_command`、`get_device_query_result`。 |
| `backend/app/agent/tool_dispatch.py` | 根调度器注册两个新工具 + schema。 |
| `backend/app/agent/chat_turn.py` | `ROOT_OPS_SYSTEM_PROMPT` 追加说明。 |
| `backend/app/schemas/hitl.py` | `HitlDecideRequest` 加 `dynamic_credential_password`。 |
| `backend/app/api/v1/hitl.py` | `decide_hitl_proposal` 校验动态凭据密码必填。 |
| `backend/app/api/router.py` | 注册策略管理路由。 |
| `backend/app/core/config.py` | 新增 `DEVICE_COMMAND_TIMEOUT_SECONDS`。 |
| `backend/init_db.py` | `SEED_PERMISSIONS` 加两条权限码。 |
| `backend/pyproject.toml` | `uv add scrapli scrapli-community`。 |
| `frontend/src/types/cmdb.ts` | 加 `vendor` 字段。 |
| `frontend/src/types/device-command-policy.ts` | 新建。 |
| `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx` | 加"厂商"下拉。 |
| `frontend/src/pages/DeviceCommandPoliciesPage.tsx` | 新建：策略管理页面。 |
| `frontend/src/components/device-command-policies/DeviceCommandPolicyFormDialog.tsx` | 新建：策略新增/编辑表单。 |
| `frontend/src/components/ops-assistant/HitlApprovalCard.tsx` | 展示 `result_excerpt`；动态凭据资产的批准表单加密码输入框。 |
| `frontend/src/lib/constants.ts` | 新增路由/权限常量。 |
| `frontend/src/components/layout/Sidebar.tsx` | "运维管理"分组加菜单项。 |

---

### Task 1: 命令目录（代码层，不入库）

**Files:**
- Create: `backend/app/agent/device_commands.py`
- Test: `backend/tests/test_device_commands.py`

**Interfaces:**
- `VendorName`、`CommandName`、`CommandType` 类型别名
- `DeviceCommandDefinition(name, version, description, command_type, templates)`
- `get_device_command(name: str) -> DeviceCommandDefinition`（未知命令名 → `UnknownDeviceCommandError`）
- `list_device_commands() -> tuple[DeviceCommandDefinition, ...]`
- `command_supports_vendor(command_name: str, vendor: str) -> bool`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_device_commands.py`：

```python
"""命令目录：只读、代码层、按厂商区分真实命令字符串。"""

import pytest

from app.agent.device_commands import (
    DEVICE_COMMAND_CATALOG_VERSION,
    UnknownDeviceCommandError,
    command_supports_vendor,
    get_device_command,
    list_device_commands,
)


def test_catalog_contains_expected_commands() -> None:
    names = {item.name for item in list_device_commands()}
    assert names == {"show_version", "show_running_config", "show_interfaces", "ping"}


def test_every_command_is_versioned_and_has_description() -> None:
    for item in list_device_commands():
        assert item.version == DEVICE_COMMAND_CATALOG_VERSION
        assert len(item.description) >= 4
        assert item.command_type in ("read_only", "state_changing")


def test_get_unknown_command_fails_closed() -> None:
    with pytest.raises(UnknownDeviceCommandError):
        get_device_command("drop_table")


def test_show_version_has_templates_for_multiple_vendors() -> None:
    definition = get_device_command("show_version")
    assert definition.templates["cisco_iosxe"] == "show version"
    assert definition.templates["huawei_vrp"] == "display version"
    assert "hp_comware" in definition.templates


def test_command_supports_vendor_reflects_template_presence() -> None:
    assert command_supports_vendor("show_version", "cisco_iosxe") is True
    assert command_supports_vendor("ping", "juniper_junos") is False


def test_command_supports_vendor_returns_false_for_unknown_command() -> None:
    assert command_supports_vendor("drop_table", "cisco_iosxe") is False


def test_catalog_is_immutable() -> None:
    definition = get_device_command("show_version")
    with pytest.raises(AttributeError):
        definition.name = "hacked"  # type: ignore[misc]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_device_commands.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.agent.device_commands'`。

- [ ] **Step 3: 实现命令目录**

创建 `backend/app/agent/device_commands.py`：

```python
"""设备诊断命令目录：唯一能决定"命令字符串到底是什么"的地方。

实现流程：
1. 数据库里的白/黑名单策略（见 app/crud/device_command_policy.py）只决定
   "要不要跳过人工审批"，不能凭空发明新命令——真正会在设备上执行的字符串
   永远来自这个模块，改动这里要走代码 review，不是运行时可配的。
2. 同一个语义命令（比如"看版本"）在不同厂商设备上的真实命令行不一样：
   思科是 show version，华为/H3C 的 VRP/Comware 是 display version。
   DeviceCommandDefinition.templates 按厂商分别登记，厂商没覆盖到就等于
   "这个厂商不支持这个命令"。
3. VendorName 定义在这里而不是 app/schemas/cmdb.py：厂商是否有效，唯一
   权威来源就是这个目录——目录里没有任何命令给这个厂商登记模板，这个厂商
   值本身就没有意义。CmdbAsset 的 vendor 字段校验从这里导入这个类型。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

type VendorName = Literal[
    "cisco_iosxe",
    "huawei_vrp",
    "hp_comware",
    "juniper_junos",
    "linux",
    "generic",
]
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
    """一条命令的完整定义：语义 + 按厂商区分的真实命令字符串。"""

    name: CommandName
    version: str
    description: str
    command_type: CommandType
    templates: Mapping[VendorName, str]


class UnknownDeviceCommandError(ValueError):
    """请求的命令名不在目录里，在分配任何资源前就该拒绝。"""


_DEVICE_COMMAND_CATALOG: dict[CommandName, DeviceCommandDefinition] = {
    "show_version": DeviceCommandDefinition(
        name="show_version",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="查看设备/系统版本信息",
        command_type="read_only",
        templates={
            "generic": "cat /etc/os-release && uname -a",
            "linux": "cat /etc/os-release && uname -a",
            "cisco_iosxe": "show version",
            "huawei_vrp": "display version",
            "hp_comware": "display version",
            "juniper_junos": "show version",
        },
    ),
    "show_running_config": DeviceCommandDefinition(
        name="show_running_config",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="查看当前生效配置（可能包含敏感信息，建议默认不进白名单）",
        command_type="read_only",
        templates={
            "cisco_iosxe": "show running-config",
            "huawei_vrp": "display current-configuration",
            "hp_comware": "display current-configuration",
            "juniper_junos": "show configuration",
        },
    ),
    "show_interfaces": DeviceCommandDefinition(
        name="show_interfaces",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="查看接口状态",
        command_type="read_only",
        templates={
            "cisco_iosxe": "show interfaces status",
            "huawei_vrp": "display interface brief",
            "hp_comware": "display interface brief",
            "juniper_junos": "show interfaces terse",
        },
    ),
    "ping": DeviceCommandDefinition(
        name="ping",
        version=DEVICE_COMMAND_CATALOG_VERSION,
        description="从设备本机发起连通性测试（固定测试网关，不接受任意目标参数，避免被当探测跳板）",
        command_type="read_only",
        templates={
            "generic": "ping -c 4 -W 2 $(ip route | awk '/default/ {print $3}')",
            "linux": "ping -c 4 -W 2 $(ip route | awk '/default/ {print $3}')",
            "cisco_iosxe": "ping <gateway>",
            "huawei_vrp": "ping <gateway>",
            "hp_comware": "ping <gateway>",
        },
    ),
}


def get_device_command(name: str) -> DeviceCommandDefinition:
    """返回目录里的一条命令定义；未知命令名在分配任何资源前失败关闭。"""
    if name not in _DEVICE_COMMAND_CATALOG:
        raise UnknownDeviceCommandError(f"unknown device command {name!r}")
    return _DEVICE_COMMAND_CATALOG[name]  # type: ignore[index]


def list_device_commands() -> tuple[DeviceCommandDefinition, ...]:
    """按目录里登记的顺序返回全部命令定义。"""
    return tuple(_DEVICE_COMMAND_CATALOG.values())


def command_supports_vendor(command_name: str, vendor: str) -> bool:
    """命令名未知，或者已知但这个厂商没有登记模板，都返回 False。"""
    if command_name not in _DEVICE_COMMAND_CATALOG:
        return False
    return vendor in _DEVICE_COMMAND_CATALOG[command_name].templates  # type: ignore[index]
```

- [ ] **Step 4: 跑测试确认通过 + 静态检查**

Run:
```bash
uv run pytest tests/test_device_commands.py -v
uv run mypy app/agent/device_commands.py
uv run ruff check app/agent/device_commands.py tests/test_device_commands.py
```
Expected: 全部通过，mypy/ruff 干净。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/device_commands.py backend/tests/test_device_commands.py
git commit -m "$(cat <<'EOF'
新增设备诊断命令目录：唯一能决定命令字符串本身的地方

- VendorName/CommandName/CommandType 三个 Literal + DeviceCommandDefinition，
  代码层、版本化，改动要走 review，不是运行时可配的
- 同一语义命令按厂商登记不同真实字符串（思科 show version vs 华为/H3C
  display version），厂商没覆盖到某条命令就等于不支持
- VendorName 定义在这里而不是 schemas 层：厂商有效性的权威来源就是这个
  目录，后续 CmdbAsset.vendor 字段直接复用这个类型做校验
EOF
)"
```

---

### Task 2: `CmdbAsset` 新增 `vendor` 字段

**Files:**
- Modify: `backend/app/models/cmdb_asset.py`
- Create: `backend/alembic/versions/2026_08_13_1000-b4e6f2a1c893_cmdb_asset_vendor.py`
- Modify: `backend/app/schemas/cmdb.py`
- Modify: `backend/app/api/v1/cmdb.py`（确认透传即可，大概率不需要改代码，只需要确认）
- Modify: `frontend/src/types/cmdb.ts`
- Modify: `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx`
- Modify: `frontend/src/components/cmdb/cmdbAssetFormSchema.ts`
- Test: `backend/tests/test_ops_models.py`、`backend/tests/test_cmdb_schemas.py`、`backend/tests/test_cmdb_api.py`

**Interfaces:**
- `CmdbAsset.vendor: str`（default `""`）
- `CmdbAssetCreate`/`Update`/`Response` 加 `vendor: VendorName`（Create 必填，Update 可选，Response 直接回显）

- [ ] **Step 1: 写失败测试（后端）**

在 `backend/tests/test_ops_models.py` 追加：

```python
async def test_cmdb_asset_vendor_defaults_to_empty_string(db_session: AsyncSession) -> None:
    asset = CmdbAsset(asset_type="switch", hostname="sw-vendor-01", ip_address="10.0.0.95")
    db_session.add(asset)
    await db_session.flush()

    assert asset.vendor == ""
```

在 `backend/tests/test_cmdb_schemas.py` 追加：

```python
def test_create_requires_valid_vendor() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(_base_create_kwargs(vendor="totally_made_up"))


def test_create_accepts_catalog_vendor() -> None:
    payload = CmdbAssetCreate.model_validate(_base_create_kwargs(vendor="huawei_vrp"))
    assert payload.vendor == "huawei_vrp"
```

（`_base_create_kwargs` 是这个测试文件里已有的辅助函数，加 `vendor` 之后需要在辅助函数默认值里也补一个合法值，比如 `"generic"`，否则其它已有测试会因为缺 `vendor` 必填字段而集体报错——这一步顺手改掉。）

在 `backend/tests/test_cmdb_api.py` 里，所有构造资产创建请求体的地方（`json={...}`）都要补 `"vendor": "generic"`（或具体厂商），否则会因为新增的必填字段而 422——这是这一步里工作量最大的部分，逐个测试函数过一遍加上。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_ops_models.py tests/test_cmdb_schemas.py tests/test_cmdb_api.py -v`
Expected: 新加的测试 FAIL（`AttributeError`/`ValidationError` 缺字段），已有测试也会因为请求体缺 `vendor` 而失败——这是预期的 RED，Step 4 会一起修好。

- [ ] **Step 3: 模型 + 迁移**

修改 `backend/app/models/cmdb_asset.py`，在 `notes` 和 `credential_type` 之间插入：

```python
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    vendor: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    credential_type: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
```

确认当前 head：

Run: `uv run alembic heads`
Expected: `e7a3c9d1f582 (head)`

创建 `backend/alembic/versions/2026_08_13_1000-b4e6f2a1c893_cmdb_asset_vendor.py`：

```python
"""Add vendor column to cmdb_assets.

Revision ID: b4e6f2a1c893
Revises: e7a3c9d1f582
Create Date: 2026-08-13 10:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "b4e6f2a1c893"
down_revision: str | None = "e7a3c9d1f582"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_destructive_downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    op.add_column(
        "cmdb_assets",
        sa.Column("vendor", sa.String(length=50), nullable=False, server_default=""),
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_column("cmdb_assets", "vendor")
```

- [ ] **Step 4: Schema + API**

修改 `backend/app/schemas/cmdb.py`：顶部 import 加 `from app.agent.device_commands import VendorName`；`CmdbAssetCreate` 加 `vendor: VendorName`（必填，放在 `asset_type` 后面）；`CmdbAssetUpdate` 加 `vendor: VendorName | None = None`；`CmdbAssetResponse` 加 `vendor: str`（响应侧用普通 `str` 就够，不需要强校验，因为是回显已经存进库的值，不是接收外部输入）。

修改 `backend/app/api/v1/cmdb.py`：`_to_response()` 加 `vendor=asset.vendor,`；`create_asset`/`update_asset` 不需要额外改动（`asset_in.model_dump()`/`model_dump(exclude_unset=True)` 已经会自动带上 `vendor`，`cmdb_asset_crud.create`/`update` 走的是 `CRUDBase` 的通用 setattr，新字段自动生效）——实施时确认一下就行，大概率不用改代码。

- [ ] **Step 5: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_ops_models.py tests/test_cmdb_schemas.py tests/test_cmdb_api.py tests/test_cmdb_asset_management_integration.py -v
uv run alembic heads
uv run mypy app
uv run ruff check .
```
Expected: 全部通过；`alembic heads` 输出 `b4e6f2a1c893 (head)`；mypy/ruff 干净。

- [ ] **Step 6: 前端**

修改 `frontend/src/types/cmdb.ts`：`CmdbAsset`/`CmdbAssetCreate`/`CmdbAssetUpdate` 加 `vendor: string`（Create 必填，Update 可选 `vendor?: string`）。

修改 `frontend/src/components/cmdb/cmdbAssetFormSchema.ts`：`formSchema` 加一个 `vendor` 字段（`z.enum([...])`，值跟后端 `VendorName` 保持一致，注释里写清楚"这份列表要跟后端 `app/agent/device_commands.py::VendorName` 手动保持一致，后端才是权威来源"）。

修改 `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx`：在"资产类型"下拉旁边加"厂商"下拉（`Select`，选项：无/思科 IOS-XE/华为 VRP/H3C Comware/Juniper Junos/Linux/通用，映射到 `cisco_iosxe`/`huawei_vrp`/`hp_comware`/`juniper_junos`/`linux`/`generic`），`defaultValues`/`handleSubmit` 一并补上。

- [ ] **Step 7: 前端验证**

Run（从 `frontend/`）:
```bash
npm run typecheck
npm run lint
npm run test
```
Expected: 干净通过。

- [ ] **Step 8: 提交**

```bash
git add backend/app/models/cmdb_asset.py backend/alembic/versions/2026_08_13_1000-b4e6f2a1c893_cmdb_asset_vendor.py backend/app/schemas/cmdb.py backend/app/api/v1/cmdb.py backend/tests/test_ops_models.py backend/tests/test_cmdb_schemas.py backend/tests/test_cmdb_api.py frontend/src/types/cmdb.ts frontend/src/components/cmdb/cmdbAssetFormSchema.ts frontend/src/components/cmdb/CmdbAssetFormDialog.tsx
git commit -m "$(cat <<'EOF'
CmdbAsset 新增 vendor 字段，供设备命令按厂商选真实语法

- asset_type 是设备大类（switch/router），不含厂商信息；vendor 跟它平级，
  值必须命中 app/agent/device_commands.py 的命令目录，填错会在真正执行
  设备命令时才发现选错了驱动/模板，所以用 Literal 强校验而不是自由字符串
- CmdbAssetResponse 侧用普通 str 回显，不重复做强校验（已经是库里的值）
- 前端表单加厂商下拉，跟资产类型平级
EOF
)"
```

---

### Task 3: `device_command_policies` 表

**Files:**
- Create: `backend/app/models/device_command_policy.py`
- Create: `backend/alembic/versions/2026_08_13_1030-f19a7c3e6d84_device_command_policies.py`
- Test: `backend/tests/test_device_command_policy_model.py`

**Interfaces:**
- `DeviceCommandPolicy(id, scope, asset_type, asset_id, command_name, decision, note, created_by_user_id, created_at, updated_at, is_deleted)`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_device_command_policy_model.py`：

```python
"""device_command_policies 模型：两种 scope 都能落库，唯一约束生效。"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud
from app.models.device_command_policy import DeviceCommandPolicy

pytestmark = pytest.mark.asyncio


async def test_asset_type_scope_policy_round_trips(db_session: AsyncSession) -> None:
    policy = DeviceCommandPolicy(
        scope="asset_type",
        asset_type="switch",
        command_name="show_version",
        decision="whitelist",
    )
    db_session.add(policy)
    await db_session.flush()

    assert policy.id is not None
    assert policy.asset_id is None
    assert policy.note == ""
    assert policy.is_deleted is False


async def test_asset_scope_policy_requires_real_asset(db_session: AsyncSession) -> None:
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": "sw-policy-01",
            "ip_address": "10.0.0.96",
            "vendor": "cisco_iosxe",
        },
    )
    await db_session.flush()

    policy = DeviceCommandPolicy(
        scope="asset",
        asset_id=asset.id,
        command_name="show_running_config",
        decision="blacklist",
    )
    db_session.add(policy)
    await db_session.flush()

    assert policy.asset_type is None


async def test_duplicate_asset_type_scope_policy_is_rejected(db_session: AsyncSession) -> None:
    db_session.add(
        DeviceCommandPolicy(
            scope="asset_type", asset_type="switch", command_name="ping", decision="whitelist"
        )
    )
    await db_session.flush()

    db_session.add(
        DeviceCommandPolicy(
            scope="asset_type", asset_type="switch", command_name="ping", decision="blacklist"
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_device_command_policy_model.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.models.device_command_policy'`。

- [ ] **Step 3: 实现模型**

创建 `backend/app/models/device_command_policy.py`：

```python
"""设备命令白/黑名单策略——只决定"要不要跳过审批"，不决定命令内容。

命令字符串本身固定在 app/agent/device_commands.py 这个代码层目录里；这张
表只是给一个 (设备类型 或 单台设备, 命令名) 组合打一个 whitelist/blacklist
标签。单台设备的策略永远覆盖设备类型级别的策略，不管方向，查找顺序见
app/crud/device_command_policy.py::resolve_policy。
"""

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DeviceCommandPolicy(Base, TimestampMixin):
    """一条设备命令白/黑名单策略。"""

    __tablename__ = "device_command_policies"
    __table_args__ = (
        Index(
            "ix_device_command_policies_asset_type_command",
            "asset_type",
            "command_name",
            unique=True,
            sqlite_where=(
                (Base.metadata.tables.get("device_command_policies").c.scope == "asset_type")
                if "device_command_policies" in Base.metadata.tables
                else None
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    asset_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    asset_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cmdb_assets.id", ondelete="CASCADE"), nullable=True
    )
    command_name: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    def __repr__(self) -> str:
        return (
            f"<DeviceCommandPolicy(id={self.id}, scope={self.scope!r}, "
            f"command={self.command_name!r}, decision={self.decision!r})>"
        )
```

`__table_args__` 里那个条件唯一索引写法比较绕（SQLite 部分索引语法），**实施时如果这段代码跑不通（大概率会，`Base.metadata.tables` 在类定义阶段还没注册当前表），改成更简单可靠的做法**：不在模型层加部分唯一索引，唯一性校验放到 `app/crud/device_command_policy.py` 的 `create()` 里用应用层查询显式检查（查一遍是否已存在相同 `(scope, asset_type/asset_id, command_name)` 的未删除记录，存在就抛自定义异常），这样也更容易给出清晰的中文错误提示，不用依赖数据库报出的 `IntegrityError` 再解析。**上面 Step 1 的 `test_duplicate_asset_type_scope_policy_is_rejected` 测试要跟着这个决定调整**——如果唯一性检查挪到 CRUD 层，这个测试要挪到 Task 4（CRUD）里用 `device_command_policy_crud.create()` 两次来验证，而不是直接对模型做两次 `db.flush()`。实施者先读一下当前 SQLAlchemy/项目里其它模型有没有类似部分唯一索引的先例（`grep -rn "sqlite_where\|postgresql_where" backend/app/models/`），没有先例就直接走"CRUD 层应用查询校验"这条更简单的路，不要在模型层死磕部分索引写法。

**已知取舍**：走 CRUD 层校验意味着数据库层面不再有唯一约束兜底，理论上两个并发创建请求都可能在对方提交前各自通过应用层查重检查，最终各自插入一条冲突策略。这个策略管理页面只有拿到 `device_command_policy:manage` 权限的管理员会用，操作频率低、天然不并发，接受这个已知风险，不在这个任务里额外加数据库锁或重试逻辑。

- [ ] **Step 4: 迁移**

确认当前 head（应该是 Task 2 结束后的 `b4e6f2a1c893`）：

Run: `uv run alembic heads`

创建 `backend/alembic/versions/2026_08_13_1030-f19a7c3e6d84_device_command_policies.py`：

```python
"""Add device_command_policies table.

Revision ID: f19a7c3e6d84
Revises: b4e6f2a1c893
Create Date: 2026-08-13 10:30:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "f19a7c3e6d84"
down_revision: str | None = "b4e6f2a1c893"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_destructive_downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("allow-destructive", "").casefold() != "true":
        raise RuntimeError(
            "Destructive downgrade blocked; rerun with "
            "'-x allow-destructive=true' after verifying the database target"
        )


def upgrade() -> None:
    op.create_table(
        "device_command_policies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("asset_type", sa.String(length=50), nullable=True),
        sa.Column("asset_id", sa.Integer(), nullable=True),
        sa.Column("command_name", sa.String(length=100), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["asset_id"], ["cmdb_assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_device_command_policies_is_deleted", "device_command_policies", ["is_deleted"]
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_index("ix_device_command_policies_is_deleted", table_name="device_command_policies")
    op.drop_table("device_command_policies")
```

实施时对照 `backend/app/models/base.py::TimestampMixin` 的真实字段定义确认 `created_at`/`updated_at` 的 `server_default`/类型写法一致（照抄 Task 1 CMDB 计划里同样步骤的做法）。

- [ ] **Step 5: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_device_command_policy_model.py -v
uv run alembic heads
uv run mypy app/models/device_command_policy.py
uv run ruff check app/models/device_command_policy.py tests/test_device_command_policy_model.py
```
Expected: 通过（如果 Step 3 改成了 CRUD 层校验唯一性，这一步先只跑前两个"能落库"的测试，第三个挪到 Task 4）；`alembic heads` 输出 `f19a7c3e6d84 (head)`。

- [ ] **Step 6: 提交**

```bash
git add backend/app/models/device_command_policy.py backend/alembic/versions/2026_08_13_1030-f19a7c3e6d84_device_command_policies.py backend/tests/test_device_command_policy_model.py
git commit -m "$(cat <<'EOF'
新增 device_command_policies 表：设备命令白/黑名单策略

- scope 区分 asset_type（设备类型级别默认策略）和 asset（单台设备覆盖），
  查找优先级（单台覆盖类型级别）由 CRUD 层的 resolve_policy 实现，模型
  本身不编码这个逻辑
- 只存策略决定（whitelist/blacklist），命令字符串本身仍然来自
  app/agent/device_commands.py 这个代码层目录，这张表改不了执行内容
EOF
)"
```

---

### Task 4: `DeviceCommandPolicy` CRUD

**Files:**
- Create: `backend/app/crud/device_command_policy.py`
- Test: `backend/tests/test_device_command_policy_crud.py`

**Interfaces:**
- `device_command_policy_crud.create(db, obj_data) -> DeviceCommandPolicy`（含唯一性校验，见 Task 3 Step 3 的实施决定）
- `device_command_policy_crud.resolve_policy(db, *, asset_id, asset_type, command_name) -> str | None`
- `device_command_policy_crud.get_multi_filtered(db, *, skip=0, limit=10) -> tuple[list[...], int]`
- `device_command_policy_crud.get_deleted_multi(db, *, skip=0, limit=10) -> tuple[list[...], int]`
- `device_command_policy_crud.restore(db, id) -> DeviceCommandPolicy | None`
- `device_command_policy_crud.hard_delete(db, id) -> bool`

**这个任务里最重要的是 `resolve_policy` 的优先级测试，务必覆盖"单台设备覆盖设备类型级别"这个规则的两个方向（单台白名单覆盖类型黑名单；单台黑名单覆盖类型白名单）。**

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_device_command_policy_crud.py`：

```python
"""DeviceCommandPolicy CRUD：唯一性校验 + resolve_policy 优先级。"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import (
    DuplicateDeviceCommandPolicyError,
    device_command_policy_crud,
)

pytestmark = pytest.mark.asyncio


async def _make_asset(db_session: AsyncSession, hostname: str = "sw-crud-01") -> int:
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": hostname,
            "ip_address": "10.0.0.97",
            "vendor": "cisco_iosxe",
        },
    )
    await db_session.flush()
    return asset.id


async def test_create_rejects_duplicate_asset_type_scope(db_session: AsyncSession) -> None:
    await device_command_policy_crud.create(
        db_session,
        {"scope": "asset_type", "asset_type": "switch", "command_name": "ping", "decision": "whitelist"},
    )
    with pytest.raises(DuplicateDeviceCommandPolicyError):
        await device_command_policy_crud.create(
            db_session,
            {"scope": "asset_type", "asset_type": "switch", "command_name": "ping", "decision": "blacklist"},
        )


async def test_resolve_policy_returns_none_when_unclassified(db_session: AsyncSession) -> None:
    asset_id = await _make_asset(db_session)
    result = await device_command_policy_crud.resolve_policy(
        db_session, asset_id=asset_id, asset_type="switch", command_name="show_version"
    )
    assert result is None


async def test_resolve_policy_falls_back_to_asset_type_level(db_session: AsyncSession) -> None:
    asset_id = await _make_asset(db_session)
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset_type",
            "asset_type": "switch",
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )
    result = await device_command_policy_crud.resolve_policy(
        db_session, asset_id=asset_id, asset_type="switch", command_name="show_version"
    )
    assert result == "whitelist"


async def test_asset_level_whitelist_overrides_asset_type_blacklist(
    db_session: AsyncSession,
) -> None:
    asset_id = await _make_asset(db_session)
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset_type",
            "asset_type": "switch",
            "command_name": "show_running_config",
            "decision": "blacklist",
        },
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_running_config",
            "decision": "whitelist",
        },
    )
    result = await device_command_policy_crud.resolve_policy(
        db_session, asset_id=asset_id, asset_type="switch", command_name="show_running_config"
    )
    assert result == "whitelist"


async def test_asset_level_blacklist_overrides_asset_type_whitelist(
    db_session: AsyncSession,
) -> None:
    asset_id = await _make_asset(db_session)
    await device_command_policy_crud.create(
        db_session,
        {"scope": "asset_type", "asset_type": "switch", "command_name": "ping", "decision": "whitelist"},
    )
    await device_command_policy_crud.create(
        db_session,
        {"scope": "asset", "asset_id": asset_id, "command_name": "ping", "decision": "blacklist"},
    )
    result = await device_command_policy_crud.resolve_policy(
        db_session, asset_id=asset_id, asset_type="switch", command_name="ping"
    )
    assert result == "blacklist"


async def test_resolve_policy_ignores_other_assets_asset_level_rule(
    db_session: AsyncSession,
) -> None:
    asset_a = await _make_asset(db_session, "sw-crud-a")
    asset_b = await _make_asset(db_session, "sw-crud-b")
    await device_command_policy_crud.create(
        db_session,
        {"scope": "asset", "asset_id": asset_a, "command_name": "ping", "decision": "whitelist"},
    )
    result = await device_command_policy_crud.resolve_policy(
        db_session, asset_id=asset_b, asset_type="switch", command_name="ping"
    )
    assert result is None


async def test_soft_delete_restore_and_hard_delete_round_trip(db_session: AsyncSession) -> None:
    policy = await device_command_policy_crud.create(
        db_session,
        {"scope": "asset_type", "asset_type": "router", "command_name": "ping", "decision": "whitelist"},
    )
    assert await device_command_policy_crud.soft_delete(db_session, policy.id) is True

    deleted, total = await device_command_policy_crud.get_deleted_multi(db_session)
    assert total == 1
    assert deleted[0].id == policy.id

    restored = await device_command_policy_crud.restore(db_session, policy.id)
    assert restored is not None

    assert await device_command_policy_crud.soft_delete(db_session, policy.id) is True
    assert await device_command_policy_crud.hard_delete(db_session, policy.id) is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_device_command_policy_crud.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.crud.device_command_policy'`。

- [ ] **Step 3: 实现**

创建 `backend/app/crud/device_command_policy.py`：

```python
"""CRUD for device command whitelist/blacklist policies."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase, ModelData
from app.models.device_command_policy import DeviceCommandPolicy


class DuplicateDeviceCommandPolicyError(ValueError):
    """同一个 (scope, asset_type 或 asset_id, command_name) 已经有一条未删除的策略。"""


class CRUDDeviceCommandPolicy(CRUDBase[DeviceCommandPolicy]):
    """设备命令策略持久化；create 额外做唯一性校验。"""

    model = DeviceCommandPolicy

    async def _find_conflicting(
        self, db: AsyncSession, obj_data: ModelData
    ) -> DeviceCommandPolicy | None:
        data = dict(obj_data)
        stmt = select(DeviceCommandPolicy).where(
            DeviceCommandPolicy.scope == data["scope"],
            DeviceCommandPolicy.command_name == data["command_name"],
            DeviceCommandPolicy.is_deleted.is_(False),
        )
        if data["scope"] == "asset_type":
            stmt = stmt.where(DeviceCommandPolicy.asset_type == data.get("asset_type"))
        else:
            stmt = stmt.where(DeviceCommandPolicy.asset_id == data.get("asset_id"))
        return (await db.execute(stmt)).scalar_one_or_none()

    async def create(self, db: AsyncSession, obj_data: ModelData) -> DeviceCommandPolicy:
        """在通用 create 之前先做唯一性检查，避免同一目标+命令出现两条冲突策略。"""
        conflict = await self._find_conflicting(db, obj_data)
        if conflict is not None:
            raise DuplicateDeviceCommandPolicyError(
                f"该目标已有一条 {obj_data['command_name']!r} 的策略（决定：{conflict.decision}）"
            )
        return await super().create(db, obj_data)

    async def resolve_policy(
        self, db: AsyncSession, *, asset_id: int, asset_type: str, command_name: str
    ) -> str | None:
        """单台设备策略优先于设备类型策略；都没有则返回 None（表示未分类）。"""
        asset_stmt = select(DeviceCommandPolicy).where(
            DeviceCommandPolicy.scope == "asset",
            DeviceCommandPolicy.asset_id == asset_id,
            DeviceCommandPolicy.command_name == command_name,
            DeviceCommandPolicy.is_deleted.is_(False),
        )
        asset_policy = (await db.execute(asset_stmt)).scalar_one_or_none()
        if asset_policy is not None:
            return asset_policy.decision

        type_stmt = select(DeviceCommandPolicy).where(
            DeviceCommandPolicy.scope == "asset_type",
            DeviceCommandPolicy.asset_type == asset_type,
            DeviceCommandPolicy.command_name == command_name,
            DeviceCommandPolicy.is_deleted.is_(False),
        )
        type_policy = (await db.execute(type_stmt)).scalar_one_or_none()
        return type_policy.decision if type_policy is not None else None

    async def get_multi_filtered(
        self, db: AsyncSession, *, skip: int = 0, limit: int = 10
    ) -> tuple[list[DeviceCommandPolicy], int]:
        """Return a page of active policies for the management page."""
        stmt = self._active_statement()
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await db.execute(count_stmt)).scalar_one()
        page_stmt = stmt.order_by(DeviceCommandPolicy.id.desc()).offset(skip).limit(limit)
        policies = list((await db.execute(page_stmt)).scalars().all())
        return policies, total

    async def get_deleted_multi(
        self, db: AsyncSession, *, skip: int = 0, limit: int = 10
    ) -> tuple[list[DeviceCommandPolicy], int]:
        """Return a page of soft-deleted policies for the recycle bin."""
        stmt = select(DeviceCommandPolicy).where(DeviceCommandPolicy.is_deleted.is_(True))
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await db.execute(count_stmt)).scalar_one()
        page_stmt = (
            stmt.order_by(DeviceCommandPolicy.updated_at.desc(), DeviceCommandPolicy.id.desc())
            .offset(skip)
            .limit(limit)
        )
        policies = list((await db.execute(page_stmt)).scalars().all())
        return policies, total

    async def restore(self, db: AsyncSession, id: int) -> DeviceCommandPolicy | None:
        """Restore a soft-deleted policy."""
        stmt = (
            select(DeviceCommandPolicy)
            .where(DeviceCommandPolicy.id == id, DeviceCommandPolicy.is_deleted.is_(True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        policy = (await db.execute(stmt)).scalar_one_or_none()
        if policy is None:
            return None
        policy.is_deleted = False
        await db.flush()
        return policy

    async def hard_delete(self, db: AsyncSession, id: int) -> bool:
        """Permanently remove a soft-deleted policy."""
        stmt = (
            select(DeviceCommandPolicy)
            .where(DeviceCommandPolicy.id == id, DeviceCommandPolicy.is_deleted.is_(True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        policy = (await db.execute(stmt)).scalar_one_or_none()
        if policy is None:
            return False
        await db.delete(policy)
        await db.flush()
        return True


device_command_policy_crud = CRUDDeviceCommandPolicy()
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_device_command_policy_crud.py -v
uv run mypy app/crud/device_command_policy.py
uv run ruff check app/crud/device_command_policy.py tests/test_device_command_policy_crud.py
```
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/crud/device_command_policy.py backend/tests/test_device_command_policy_crud.py
git commit -m "$(cat <<'EOF'
新增 DeviceCommandPolicy CRUD，含策略优先级解析

- create 先做应用层唯一性校验（同一目标+命令只能有一条策略），比数据库
  报 IntegrityError 更容易给出清晰的拒绝原因
- resolve_policy 是这次的核心查询：单台设备策略永远覆盖设备类型级别策略，
  不管方向；都没有则返回 None，表示"未分类，走人工审批"
- 回收站三件套（get_deleted_multi/restore/hard_delete）照抄 CmdbAsset 那次
  的模式，管理页面交互保持一致
EOF
)"
```

---

### Task 5: `HitlProposal` 支持 `device_query` 动作类型

**Files:**
- Modify: `backend/app/agent/hitl.py`
- Test: `backend/tests/test_agent_hitl.py`

**Interfaces:**
- `ActionType = Literal["notify", "device_control", "device_query"]`
- `DeviceQueryPayload(command_name: str)`
- `ProposalSafeSummary` 加 `result_excerpt: str | None = None`

**这一步只加数据结构和校验分支，不接真正的策略解析/自动批准/执行器（那是 Task 7）——先保证 `propose_action` 能对 `device_query` 类型做基本的载荷校验并落一条 `PENDING` 记录，跟现有 `device_control` 今天的行为一致（写死不自动批准），后面 Task 7 再把自动批准分支接上。**

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_agent_hitl.py` 追加：

```python
async def test_propose_device_query_creates_pending_proposal(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id, asset_id = await _make_session_and_asset(db_session, test_user.id)  # 复用文件里已有的辅助函数

    summary = await propose_action(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_query",
        asset_id=asset_id,
        payload={"command_name": "show_version"},
        reason="排查交换机异常",
        actor_user_id=test_user.id,
    )

    assert summary.status == "PENDING"
    assert summary.action_type == "device_query"


async def test_propose_device_query_rejects_extra_payload_fields(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id, asset_id = await _make_session_and_asset(db_session, test_user.id)

    with pytest.raises(HitlProposalRejectedError):
        await propose_action(
            db_session,
            session_id=session_id,
            proposed_by_agent_id=None,
            action_type="device_query",
            asset_id=asset_id,
            payload={"command_name": "show_version", "extra_field": "nope"},
            reason="排查交换机异常",
            actor_user_id=test_user.id,
        )


async def test_proposal_safe_summary_includes_result_excerpt_field() -> None:
    from dataclasses import fields

    field_names = {f.name for f in fields(ProposalSafeSummary)}
    assert "result_excerpt" in field_names
```

具体 `_make_session_and_asset` 辅助函数名字/签名以文件里已有的写法为准，如果没有现成的，参照文件里其它测试怎么准备 `session_id`/`asset_id` 照抄。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_agent_hitl.py -k device_query -v`
Expected: FAIL（`action_type` 不接受 `"device_query"`，或者 `ProposalSafeSummary` 没有 `result_excerpt` 字段）。

- [ ] **Step 3: 实现**

修改 `backend/app/agent/hitl.py`：

1. `ActionType` 改成：
```python
type ActionType = Literal["notify", "device_control", "device_query"]
```

2. 在 `DeviceControlPayload` 后面加：
```python
class DeviceQueryPayload(BaseModel):
    """设备诊断查询动作的严格载荷。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    command_name: str = Field(min_length=1, max_length=100)
```

3. `ProposalSafeSummary` 加一个默认字段：
```python
@dataclass(frozen=True, slots=True)
class ProposalSafeSummary:
    """可安全返回给 Agent 或前端事件层的提案摘要。"""

    proposal_id: int
    action_type: ActionType
    status: str
    reason: str
    asset_id: int | None
    result_excerpt: str | None = None
```

4. `_summary()` 里，`if proposal.action_type not in ("notify", "device_control"):` 改成 `if proposal.action_type not in ("notify", "device_control", "device_query"):`；并在构造 `ProposalSafeSummary` 时加一行读取（先给个占位实现，Task 7 会补全真正写入的地方）：
```python
    raw_result_excerpt = payload.get("last_result_excerpt")
    result_excerpt = raw_result_excerpt if isinstance(raw_result_excerpt, str) else None
```
`return ProposalSafeSummary(..., result_excerpt=result_excerpt)`。

5. `_validated_payload()` 的 `if/elif` 链加一支：
```python
        elif action_type == "device_query":
            validated = DeviceQueryPayload.model_validate(candidate).model_dump()
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_agent_hitl.py -v
uv run mypy app/agent/hitl.py
uv run ruff check app/agent/hitl.py tests/test_agent_hitl.py
```
Expected: 全部通过（含已有测试，确认没有回归），mypy/ruff 干净。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/hitl.py backend/tests/test_agent_hitl.py
git commit -m "$(cat <<'EOF'
HitlProposal 支持 device_query 动作类型（数据结构与校验）

- ActionType 加第三个值；DeviceQueryPayload 只接受 command_name，跟
  notify/device_control 一样严格拒绝多余字段
- ProposalSafeSummary 加 result_excerpt（默认 None），为后续展示命令
  执行结果预留字段；这一步先只加字段，真正写入值在策略解析接上之后
- 目前 device_query 还是走"落 PENDING、不自动批准"的默认路径，跟
  device_control 今天的行为一致；自动批准分支下一个任务再接
EOF
)"
```

---

### Task 6: `DeviceQueryExecutor`（Scrapli 连接）

**Files:**
- Modify: `backend/app/agent/executors.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/.env.example`
- Test: `backend/tests/test_device_query_executor.py`

**Interfaces:**
- `settings.DEVICE_COMMAND_TIMEOUT_SECONDS: float`
- `DeviceQueryExecutor.execute(db, *, asset, command_name, dynamic_password) -> ExecutionResult`

- [ ] **Step 1: 加依赖**

Run: `uv add scrapli scrapli-community`（实施时如果这个包名在 PyPI 上不完全匹配，用 `uv add scrapli` 加上 Scrapli 官方文档当时给出的 async 相关 extras/social 包名，`scrapli-community` 是给华为等平台驱动用的社区包，两者都要装）。

- [ ] **Step 2: 加超时配置**

修改 `backend/app/core/config.py`，在 `CMDB_DIFF_INTERVAL_SECONDS` 那一行之后加：

```python
    DEVICE_COMMAND_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0, le=120)
```

修改 `backend/.env.example`，在 CMDB 相关配置说明附近加一行：

```ini
DEVICE_COMMAND_TIMEOUT_SECONDS=15.0
```

- [ ] **Step 3: 写失败测试**

创建 `backend/tests/test_device_query_executor.py`。用 `unittest.mock`/`monkeypatch` 把 Scrapli 的连接对象换成假实现，不真的发网络请求：

```python
"""DeviceQueryExecutor：凭据解析分支 + 输出截断 + 失败分类，不接真实设备。"""

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.executors import DeviceQueryExecutor
from app.core.cmdb_credential import encrypt_credential_password
from app.core.config import settings
from app.crud.cmdb_asset import cmdb_asset_crud

pytestmark = pytest.mark.asyncio


async def _make_asset(
    db_session: AsyncSession, *, credential_type: str, credential_username: str = "",
    credential_password_encrypted: str | None = None,
) -> int:
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": "sw-exec-01",
            "ip_address": "10.0.0.98",
            "vendor": "cisco_iosxe",
            "credential_type": credential_type,
            "credential_username": credential_username,
            "credential_password_encrypted": credential_password_encrypted,
        },
    )
    await db_session.flush()
    return asset.id


async def test_dynamic_credential_without_password_fails_closed(
    db_session: AsyncSession,
) -> None:
    asset_id = await _make_asset(db_session, credential_type="dynamic", credential_username="admin")
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    executor = DeviceQueryExecutor()
    result = await executor.execute(
        db_session, asset=asset, command_name="show_version", dynamic_password=None
    )

    assert result.ok is False


async def test_static_credential_decrypts_and_connects(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    secret = "Sup3rSecret!"
    ciphertext = encrypt_credential_password(secret)
    asset_id = await _make_asset(
        db_session,
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted=ciphertext,
    )
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    fake_connection = AsyncMock()
    fake_connection.send_command = AsyncMock(
        return_value=type("Resp", (), {"result": "Cisco IOS XE Software", "failed": False})()
    )
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        executor = DeviceQueryExecutor()
        result = await executor.execute(
            db_session, asset=asset, command_name="show_version", dynamic_password=None
        )

    assert result.ok is True
    assert "Cisco IOS XE" in result.detail["output"]


async def test_connection_failure_does_not_leak_raw_exception_text(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_asset(
        db_session,
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted=ciphertext,
    )
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    with patch(
        "app.agent.executors._open_scrapli_connection",
        side_effect=RuntimeError("internal topology detail: 10.9.9.9 refused"),
    ):
        executor = DeviceQueryExecutor()
        result = await executor.execute(
            db_session, asset=asset, command_name="show_version", dynamic_password=None
        )

    assert result.ok is False
    assert "10.9.9.9" not in result.message


async def test_long_output_is_truncated(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_asset(
        db_session,
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted=ciphertext,
    )
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None

    long_output = "x" * 10_000
    fake_connection = AsyncMock()
    fake_connection.send_command = AsyncMock(
        return_value=type("Resp", (), {"result": long_output, "failed": False})()
    )
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        executor = DeviceQueryExecutor()
        result = await executor.execute(
            db_session, asset=asset, command_name="show_version", dynamic_password=None
        )

    assert result.detail["truncated"] is True
    assert len(result.detail["output"]) < 10_000


def _generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()
```

- [ ] **Step 4: 跑测试确认失败**

Run: `uv run pytest tests/test_device_query_executor.py -v`
Expected: FAIL，`ImportError`/`AttributeError`（`DeviceQueryExecutor` 还不存在）。

- [ ] **Step 5: 实现执行器**

修改 `backend/app/agent/executors.py`，顶部 import 区加 Scrapli 相关导入，文件末尾加：

```python
from app.agent.device_commands import command_supports_vendor, get_device_command
from app.core.cmdb_credential import decrypt_credential_password
from app.core.config import settings
from app.models.cmdb_asset import CmdbAsset

_OUTPUT_TRUNCATE_LIMIT = 4000


def _truncate_output(text: str, *, limit: int = _OUTPUT_TRUNCATE_LIMIT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "…(截断)", True


async def _open_scrapli_connection(*, host: str, vendor: str, username: str, password: str, timeout: float):
    """建立一个已认证的 Scrapli 异步连接；抽成独立函数方便测试打桩。

    实施时按 vendor 选平台驱动：cisco_iosxe/juniper_junos 用 scrapli 核心包的
    对应 driver；huawei_vrp 用 scrapli_community 的 huawei_vrp driver；
    hp_comware/generic/linux 用 scrapli 核心包的 generic driver。
    """
    raise NotImplementedError  # 实施阶段按 Scrapli 当时的具体 API 补全


class DeviceQueryExecutor:
    """只读诊断命令执行器：解析凭据、按厂商选真实命令、跑 Scrapli、截断输出。"""

    async def execute(
        self,
        db: AsyncSession,
        *,
        asset: CmdbAsset,
        command_name: str,
        dynamic_password: str | None,
    ) -> ExecutionResult:
        """执行一次设备诊断命令查询并返回安全结果。

        Args:
            db: 当前事务的数据库会话（目前未使用，保留是为了跟其它执行器
                签名一致，也方便未来加执行前后的额外落库操作）。
            asset: 目标 CMDB 资产，须已配置 vendor 与凭据。
            command_name: 目录里的命令名，调用方保证已通过白名单/校验。
            dynamic_password: 动态凭据时的一次性明文密码；静态凭据时忽略。

        Returns:
            ok=True 时 detail 含 output/truncated；ok=False 时 message 只给
            分类信息，不透传任何原始异常文本或设备侧细节。
        """
        if asset.credential_type == "static":
            if not asset.credential_password_encrypted:
                return ExecutionResult(ok=False, message="资产未配置静态密码")
            password = decrypt_credential_password(asset.credential_password_encrypted)
        elif asset.credential_type == "dynamic":
            if not dynamic_password:
                return ExecutionResult(ok=False, message="动态凭据缺少本次输入的密码")
            password = dynamic_password
        else:
            return ExecutionResult(ok=False, message="资产未配置登录凭据")

        if not command_supports_vendor(command_name, asset.vendor):
            return ExecutionResult(ok=False, message="该设备厂商不支持这个命令")
        definition = get_device_command(command_name)
        template = definition.templates[asset.vendor]  # type: ignore[index]

        try:
            connection = await _open_scrapli_connection(
                host=asset.ip_address,
                vendor=asset.vendor,
                username=asset.credential_username,
                password=password,
                timeout=settings.DEVICE_COMMAND_TIMEOUT_SECONDS,
            )
            response = await connection.send_command(template)
        except Exception:
            return ExecutionResult(ok=False, message="连接或执行命令失败")

        if getattr(response, "failed", False):
            return ExecutionResult(ok=False, message="设备返回命令执行失败")

        output, truncated = _truncate_output(str(response.result))
        return ExecutionResult(
            ok=True,
            message="命令执行完成",
            detail={"output": output, "truncated": truncated},
        )
```

`_open_scrapli_connection` 先留 `NotImplementedError`——**这一步的测试全部通过 mock 掉这个函数，不依赖它的真实实现**，Step 6 单独补全真实 Scrapli 调用（因为 Scrapli 的具体 driver 选择/连接参数写法要在实施时对照当时的官方文档确认，写在计划里的伪代码容易过时）。

- [ ] **Step 6: 补全真实连接逻辑**

实施者此时查阅 Scrapli 当前文档（`AsyncGenericDriver`/`AsyncIOSXEDriver`/`scrapli_community` 里的 `AsyncHuaweiVRPDriver` 或等价物），把 `_open_scrapli_connection` 的 `raise NotImplementedError` 换成真实实现：按 `vendor` 选 driver 类，用 `host`/`auth_username`/`auth_password`/`auth_strict_key=False`（内网设备通常没有已知 host key，实施时确认这个决定，如果需要更严格的 host key 校验就不设 False，改成走已知 host key 文件）、`timeout_socket`/`timeout_transport` 传入 `timeout`，`await connection.open()` 后返回。

**这一步不需要新增测试**（Step 3 的 mock 测试已经覆盖了 `DeviceQueryExecutor.execute` 的分支逻辑；`_open_scrapli_connection` 本身要不要接真实网络测试，取决于实施环境有没有可用的测试设备——没有的话就靠 Step 3 的 mock 测试 + 手工验收覆盖，在 Task 11 的跨组件验收里用真实凭据对着一台测试设备跑一次作为人工确认，不写自动化测试断言真实网络行为）。

- [ ] **Step 7: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_device_query_executor.py -v
uv run mypy app/agent/executors.py
uv run ruff check app/agent/executors.py tests/test_device_query_executor.py
```
Expected: 全部通过（Step 3 的 mock 测试不受 Step 6 改动影响，因为 `_open_scrapli_connection` 本身被直接打桩替换）。

- [ ] **Step 8: 提交**

```bash
git add backend/pyproject.toml backend/uv.lock backend/app/agent/executors.py backend/app/core/config.py backend/.env.example backend/tests/test_device_query_executor.py
git commit -m "$(cat <<'EOF'
新增 DeviceQueryExecutor：解析凭据 + Scrapli 连接 + 输出截断

- 凭据分支跟 CMDB 凭据管理的既有规则对齐：static 解密库里密文，dynamic
  必须有调用方传入的一次性密码，none 直接拒绝
- 命令模板按 (command_name, asset.vendor) 从 app/agent/device_commands.py
  取，厂商不支持这个命令直接拒绝，不会拼出一个不存在的命令字符串
- 连接失败/命令失败只返回分类信息，不透传 Scrapli 原始异常文本，避免
  设备侧细节（主机名、内部拓扑）意外泄漏
- 长输出截断到 4000 字符，detail 里带 truncated 标记
- _open_scrapli_connection 抽成独立函数，测试全部用 mock 覆盖分支逻辑，
  不依赖真实网络；真实 Scrapli driver 选择逻辑单独一步补全并在 Task 11
  跨组件验收里对测试设备做一次人工确认
EOF
)"
```

---

### Task 7: `propose_action` 接入策略解析与自动批准

**Files:**
- Modify: `backend/app/agent/hitl.py`
- Test: `backend/tests/test_agent_hitl.py`

**Interfaces:**
- `propose_action` 新增对 `device_query` 的策略解析分支（黑名单拒绝、白名单自动执行、未分类走 PENDING）
- `resume_proposal` 新增 `dynamic_password: str | None = None` 参数

**这是整个功能最关键的集成点，务必按 spec §5/§6 的顺序覆盖测试：资产不存在/厂商不支持命令/无凭据/厂商为空 → 拒绝且不建提案；黑名单 → 拒绝且不建提案；白名单 + 非动态凭据 → 自动执行；白名单 + 动态凭据 → 仍停在 PENDING（这个组合最容易漏测）；未分类 → PENDING。**

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_agent_hitl.py` 追加（需要 `device_command_policy_crud`、一个真实 `CMDB_CREDENTIAL_KEY` 的 monkeypatch，以及给测试资产设置 `vendor`/凭据）：

```python
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core.cmdb_credential import encrypt_credential_password
from app.core.config import settings
from app.crud.device_command_policy import device_command_policy_crud


async def _make_query_asset(
    db_session: AsyncSession,
    *,
    credential_type: str = "static",
    credential_username: str = "admin",
    credential_password_encrypted: str | None = "placeholder",
) -> int:
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": "sw-hitl-01",
            "ip_address": "10.0.0.99",
            "vendor": "cisco_iosxe",
            "credential_type": credential_type,
            "credential_username": credential_username,
            "credential_password_encrypted": credential_password_encrypted,
        },
    )
    await db_session.flush()
    return asset.id


async def test_device_query_rejects_when_asset_has_no_credential(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session, credential_type="none", credential_password_encrypted=None)

    with pytest.raises(HitlProposalRejectedError):
        await propose_action(
            db_session, session_id=session_id, proposed_by_agent_id=None,
            action_type="device_query", asset_id=asset_id,
            payload={"command_name": "show_version"}, reason="test", actor_user_id=test_user.id,
        )


async def test_device_query_rejects_unsupported_command_for_vendor(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session)

    with pytest.raises(HitlProposalRejectedError):
        await propose_action(
            db_session, session_id=session_id, proposed_by_agent_id=None,
            action_type="device_query", asset_id=asset_id,
            payload={"command_name": "unknown_command"}, reason="test", actor_user_id=test_user.id,
        )


async def test_device_query_blacklist_rejects_without_creating_proposal(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session)
    await device_command_policy_crud.create(
        db_session,
        {"scope": "asset", "asset_id": asset_id, "command_name": "show_running_config", "decision": "blacklist"},
    )
    await db_session.commit()

    with pytest.raises(HitlProposalRejectedError):
        await propose_action(
            db_session, session_id=session_id, proposed_by_agent_id=None,
            action_type="device_query", asset_id=asset_id,
            payload={"command_name": "show_running_config"}, reason="test", actor_user_id=test_user.id,
        )

    proposals = await hitl_proposal_crud.list_for_session(db_session, session_id)
    assert proposals == []


async def test_device_query_whitelist_auto_executes_for_static_credential(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    ciphertext = encrypt_credential_password("whatever")
    asset_id = await _make_query_asset(db_session, credential_password_encrypted=ciphertext)
    await device_command_policy_crud.create(
        db_session,
        {"scope": "asset", "asset_id": asset_id, "command_name": "show_version", "decision": "whitelist"},
    )
    await db_session.commit()

    from unittest.mock import AsyncMock, patch

    fake_connection = AsyncMock()
    fake_connection.send_command = AsyncMock(
        return_value=type("Resp", (), {"result": "fake output", "failed": False})()
    )
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        summary = await propose_action(
            db_session, session_id=session_id, proposed_by_agent_id=None,
            action_type="device_query", asset_id=asset_id,
            payload={"command_name": "show_version"}, reason="test", actor_user_id=test_user.id,
        )

    assert summary.status == "EXECUTED"
    assert summary.result_excerpt is not None
    assert "fake output" in summary.result_excerpt


async def test_device_query_whitelist_still_pends_for_dynamic_credential(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(
        db_session, credential_type="dynamic", credential_password_encrypted=None
    )
    await device_command_policy_crud.create(
        db_session,
        {"scope": "asset", "asset_id": asset_id, "command_name": "show_version", "decision": "whitelist"},
    )
    await db_session.commit()

    summary = await propose_action(
        db_session, session_id=session_id, proposed_by_agent_id=None,
        action_type="device_query", asset_id=asset_id,
        payload={"command_name": "show_version"}, reason="test", actor_user_id=test_user.id,
    )

    assert summary.status == "PENDING"


async def test_device_query_unclassified_command_pends(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(db_session)

    summary = await propose_action(
        db_session, session_id=session_id, proposed_by_agent_id=None,
        action_type="device_query", asset_id=asset_id,
        payload={"command_name": "show_version"}, reason="test", actor_user_id=test_user.id,
    )

    assert summary.status == "PENDING"


async def test_resume_proposal_passes_dynamic_password_to_executor(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    asset_id = await _make_query_asset(
        db_session, credential_type="dynamic", credential_password_encrypted=None
    )
    summary = await propose_action(
        db_session, session_id=session_id, proposed_by_agent_id=None,
        action_type="device_query", asset_id=asset_id,
        payload={"command_name": "show_version"}, reason="test", actor_user_id=test_user.id,
    )
    await decide_proposal(
        db_session, proposal_id=summary.proposal_id, approve=True, reviewed_by_user_id=test_user.id
    )
    await db_session.commit()

    from unittest.mock import AsyncMock, patch

    fake_connection = AsyncMock()
    fake_connection.send_command = AsyncMock(
        return_value=type("Resp", (), {"result": "otp output", "failed": False})()
    )
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        resumed = await resume_proposal(
            db_session, proposal_id=summary.proposal_id, actor_user_id=test_user.id,
            dynamic_password="one-time-pass",
        )

    assert resumed.status == "EXECUTED"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_agent_hitl.py -k device_query -v`
Expected: FAIL（策略解析分支还没实现，`resume_proposal` 还不接受 `dynamic_password`）。

- [ ] **Step 3: 实现**

修改 `backend/app/agent/hitl.py`：

1. 顶部 import 加：
```python
from app.agent.device_commands import command_supports_vendor
from app.agent.executors import DeviceQueryExecutor
from app.crud.device_command_policy import device_command_policy_crud
```

2. `_DEVICE_CONTROL_EXECUTOR = NotImplementedExecutor()` 后面加一行：
```python
_DEVICE_QUERY_EXECUTOR = DeviceQueryExecutor()
```

3. `propose_action` 里，`asset = await cmdb_asset_crud.get(db, asset_id)` 和 `if asset is None:` 判断之后，`proposal = await hitl_proposal_crud.create(...)` 之前，插入 `device_query` 专属的前置校验：

```python
    if action_type == "device_query":
        command_name = stored_payload["command_name"]
        assert isinstance(command_name, str)
        if asset.credential_type == "none":
            raise HitlProposalRejectedError("该资产未配置登录凭据，无法执行设备命令")
        if not asset.vendor:
            raise HitlProposalRejectedError("资产未配置厂商信息，无法确定命令语法")
        if not command_supports_vendor(command_name, asset.vendor):
            raise HitlProposalRejectedError("该设备厂商不支持这个命令")
        policy_decision = await device_command_policy_crud.resolve_policy(
            db, asset_id=asset.id, asset_type=asset.asset_type, command_name=command_name
        )
        if policy_decision == "blacklist":
            raise HitlProposalRejectedError("该命令已被列入黑名单，禁止执行")
    else:
        policy_decision = None
```

4. 自动批准分支（现有的 `if action_type == "notify" and settings.HITL_NOTIFY_AUTO_APPROVE:`）改成同时支持 `device_query` 白名单：

```python
    auto_approve = (action_type == "notify" and settings.HITL_NOTIFY_AUTO_APPROVE) or (
        action_type == "device_query"
        and policy_decision == "whitelist"
        and asset.credential_type != "dynamic"
    )
    if auto_approve:
        await decide_proposal(
            db, proposal_id=proposal.id, approve=True, reviewed_by_user_id=actor_user_id, publisher=publisher,
        )
        return await resume_proposal(
            db, proposal_id=proposal.id, actor_user_id=actor_user_id, publisher=publisher,
        )
```

（注意：动态凭据资产即使命中白名单也不会进这个分支，因为条件里显式排除了 `credential_type == "dynamic"`——这是 spec 里强调的、最容易漏测的例外，Step 1 的 `test_device_query_whitelist_still_pends_for_dynamic_credential` 就是测这个。）

5. `resume_proposal` 签名加 `dynamic_password: str | None = None`；内部 `elif proposal.action_type == "device_control":` 后面加一支：

```python
        elif proposal.action_type == "device_query":
            asset_for_query = await cmdb_asset_crud.get(db, ...)  # 见下方说明
            if asset_for_query is None:
                execution_result = ExecutionResult(ok=False, message="资产不存在")
            else:
                raw_command_name = proposal.action_payload.get("command_name")
                execution_result = await _DEVICE_QUERY_EXECUTOR.execute(
                    db, asset=asset_for_query, command_name=str(raw_command_name),
                    dynamic_password=dynamic_password,
                )
```

`asset_id` 要从 `proposal.action_payload["asset_id"]` 里取（`_validated_payload` 已经把它合并进 `stored_payload` 了），实际代码应该是：
```python
            raw_asset_id = proposal.action_payload.get("asset_id")
            asset_for_query = (
                await cmdb_asset_crud.get(db, raw_asset_id) if isinstance(raw_asset_id, int) else None
            )
```

6. 执行成功后（`if execution_result.ok:` 分支里，`await hitl_proposal_crud.mark_executed(...)` 之后），如果是 `device_query` 且有截断输出，要把它写回 `action_payload`，供 `_summary()` 读取。`hitl_proposal_crud` 目前没有"更新 action_payload"的方法，**这一步实施时确认一下 `CRUDBase.update()` 能不能直接拿来用**（`await hitl_proposal_crud.update(db, proposal.id, {"action_payload": {**proposal.action_payload, "last_result_excerpt": execution_result.detail.get("output")}})`——`action_payload` 不在 `CRUDBase.update` 的 `immutable_fields` 黑名单里，应该可以直接这样用，实施时跑一下测试确认）。

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_agent_hitl.py -v
uv run mypy app/agent/hitl.py
uv run ruff check app/agent/hitl.py tests/test_agent_hitl.py
```
Expected: 全部通过，尤其确认 `test_device_query_whitelist_still_pends_for_dynamic_credential` 这条真的在测（不是因为别的原因误通过）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/hitl.py backend/tests/test_agent_hitl.py
git commit -m "$(cat <<'EOF'
propose_action 接入设备命令策略解析与自动批准

- device_query 提案创建前校验：厂商是否支持该命令、资产是否配置凭据/厂商、
  是否命中黑名单——黑名单直接拒绝，不落 PENDING 记录
- 白名单命中且资产不是动态凭据时复用现有自动批准分支（跟 notify 的
  HITL_NOTIFY_AUTO_APPROVE 走同一段代码路径），立即执行并把结果写回
  ProposalSafeSummary.result_excerpt
- 动态凭据资产无论命不命中白名单都不会进自动批准分支——没有人在场就
  拿不到密码，这是整个设计里唯一一处"策略允许但代码层仍强制人工介入"
  的例外，用专门的测试锁死这个组合
- resume_proposal 新增 dynamic_password 参数，人工审批通过时把当场输入
  的密码透传给 DeviceQueryExecutor，不落库不进审计
EOF
)"
```

---

### Task 8: `query_device_command` + `get_device_query_result` 工具

**Files:**
- Modify: `backend/app/agent/hitl_tools.py`
- Modify: `backend/app/agent/tool_dispatch.py`
- Modify: `backend/app/agent/chat_turn.py`
- Test: `backend/tests/test_agent_hitl_tools.py`、`backend/tests/test_agent_tool_dispatch.py`

**Interfaces:**
- `query_device_command(db, *, session_id, actor_user_id, proposed_by_agent_id, asset_id, command_name, reason, publisher) -> ToolResult`
- `get_device_query_result(db, *, session_id, proposal_id) -> ToolResult`
- 根调度器新增两条 schema，只挂根 Agent

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_agent_hitl_tools.py` 追加（用 `monkeypatch.setattr(hitl_tools, "propose_action", fake_propose_action)` 这种既有的隔离方式，参照文件里 `propose_remediation` 的测试写法）：

```python
async def test_query_device_command_returns_pending_when_not_executed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_propose_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        return ProposalSafeSummary(
            proposal_id=51, action_type="device_query", status="PENDING",
            reason="排查交换机", asset_id=9,
        )

    monkeypatch.setattr(hitl_tools, "propose_action", fake_propose_action)

    result = await hitl_tools.query_device_command(
        db_session, session_id=1, actor_user_id=2, proposed_by_agent_id=None,
        asset_id=9, command_name="show_version", reason="排查交换机",
    )

    assert result.control == "pending_approval"
    assert "51" in result.content


async def test_query_device_command_returns_ok_with_result_when_auto_executed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_propose_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        return ProposalSafeSummary(
            proposal_id=52, action_type="device_query", status="EXECUTED",
            reason="排查交换机", asset_id=9, result_excerpt="fake device output",
        )

    monkeypatch.setattr(hitl_tools, "propose_action", fake_propose_action)

    result = await hitl_tools.query_device_command(
        db_session, session_id=1, actor_user_id=2, proposed_by_agent_id=None,
        asset_id=9, command_name="show_version", reason="排查交换机",
    )

    assert result.control == "ok"
    assert "fake device output" in result.content


async def test_get_device_query_result_scopes_to_session(db_session: AsyncSession) -> None:
    session_id, asset_id = await _make_session_and_asset(db_session)  # 复用/参照既有辅助函数
    other_session_id, _ = await _make_session_and_asset(db_session)

    proposal = await hitl_proposal_crud.create(
        db_session, session_id=session_id, proposed_by_agent_id=None,
        action_type="device_query", action_payload={"asset_id": asset_id, "command_name": "show_version"},
    )
    await db_session.commit()

    same_session = await hitl_tools.get_device_query_result(
        db_session, session_id=session_id, proposal_id=proposal.id
    )
    assert "不存在" not in same_session.content

    other_session = await hitl_tools.get_device_query_result(
        db_session, session_id=other_session_id, proposal_id=proposal.id
    )
    assert other_session.control == "rejected"
```

具体辅助函数/fixture 名字以文件里已有的写法为准，实施时先读一下文件顶部的 import 和已有测试函数确认。

在 `backend/tests/test_agent_tool_dispatch.py`（或 `test_hitl_integration.py`，看根调度器测试放在哪个文件）追加：子 Agent 的 `build_tool_dispatcher` 不认识 `query_device_command`（参照 `test_scenario_c_child_dispatcher_rejects_propose_remediation` 的写法）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_agent_hitl_tools.py -k device_query -v`
Expected: FAIL，`AttributeError`（函数还不存在）。

- [ ] **Step 3: 实现工具**

修改 `backend/app/agent/hitl_tools.py`，文件末尾加：

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
) -> ToolResult:
    """对已配置凭据的资产发起只读诊断命令查询。

    白名单命中且资产非动态凭据时会在这次调用里直接执行完成，返回 ok 并
    附带命令输出；其它情况停在 pending_approval，需要人工审批（动态凭据
    资产还需要在批准时当场输入密码）。
    """
    try:
        summary = await propose_action(
            db, session_id=session_id, actor_user_id=actor_user_id,
            proposed_by_agent_id=proposed_by_agent_id, asset_id=asset_id,
            action_type="device_query", payload={"command_name": command_name},
            reason=reason, publisher=publisher,
        )
    except HitlProposalRejectedError as exc:
        return ToolResult(control="rejected", content=f"设备命令查询被拒绝：{exc}")
    except Exception as exc:
        return ToolResult(control="failed", content=f"设备命令查询创建失败：{type(exc).__name__}")

    if summary.status == "EXECUTED":
        output = summary.result_excerpt or "（无输出）"
        return ToolResult(
            control="ok",
            content=f"设备命令 {summary.proposal_id} 已自动批准并执行：\n{output}",
        )
    if summary.status == "PENDING":
        return ToolResult(
            control="pending_approval",
            content=f"设备命令查询 {summary.proposal_id} 已创建，正在等待人工审批。",
        )
    return ToolResult(
        control="failed",
        content=f"设备命令查询 {summary.proposal_id} 当前状态为 {summary.status}，未完成执行。",
    )


async def get_device_query_result(
    db: AsyncSession, *, session_id: int, proposal_id: int
) -> ToolResult:
    """按会话回查一个已提交的设备命令查询提案的当前结果。

    只读、无审批要求；跟别的会话的提案严格隔离，不匹配当成"不存在"处理，
    不泄露其它会话的提案是否存在。
    """
    from app.crud.hitl_proposal import hitl_proposal_crud

    proposal = await hitl_proposal_crud.get(db, proposal_id)
    if proposal is None or proposal.session_id != session_id:
        return ToolResult(control="rejected", content="提案不存在")

    if proposal.status == "EXECUTED":
        excerpt = proposal.action_payload.get("last_result_excerpt") or "（无输出）"
        return ToolResult(control="ok", content=f"提案 {proposal_id} 已执行：\n{excerpt}")
    if proposal.status == "REJECTED":
        return ToolResult(control="ok", content=f"提案 {proposal_id} 已被拒绝")
    if proposal.status in ("PENDING", "APPROVED"):
        return ToolResult(control="ok", content=f"提案 {proposal_id} 还未执行完成，当前状态：{proposal.status}")
    return ToolResult(control="ok", content=f"提案 {proposal_id} 当前状态：{proposal.status}")
```

修改 `backend/app/agent/tool_dispatch.py`：

1. import 区加：
```python
from app.agent.hitl_tools import get_device_query_result, propose_remediation, query_device_command
```
（原来只 import `propose_remediation`，改成同一行三个一起 import。）

2. 加两个参数模型（放在 `ProposeRemediationArgs` 后面）：
```python
class QueryDeviceCommandArgs(_Args):
    asset_id: int = Field(ge=1)
    command_name: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


class GetDeviceQueryResultArgs(_Args):
    proposal_id: int = Field(ge=1)
```

3. `root_tool_schemas()` 里 `parameters = deepcopy(ProposeRemediationArgs.model_json_schema())` 那部分改成给三个工具都生成 schema，返回列表加两条：
```python
def root_tool_schemas() -> list[dict[str, Any]]:
    """返回根 Agent 的七个只读工具、整改提案工具和两个设备命令工具 Schema。"""
    propose_parameters = deepcopy(ProposeRemediationArgs.model_json_schema())
    propose_parameters.pop("title", None)
    query_parameters = deepcopy(QueryDeviceCommandArgs.model_json_schema())
    query_parameters.pop("title", None)
    result_parameters = deepcopy(GetDeviceQueryResultArgs.model_json_schema())
    result_parameters.pop("title", None)
    return [
        *tool_schemas_for(_ROOT_READ_ONLY_TOOLS),
        {
            "type": "function",
            "function": {
                "name": "propose_remediation",
                "description": f"[{ROOT_TOOL_SCHEMA_VERSION}] 为指定资产创建需人工审批的整改提案。",
                "parameters": propose_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_device_command",
                "description": (
                    f"[{ROOT_TOOL_SCHEMA_VERSION}] 对已配置凭据的资产发起只读诊断命令查询"
                    "（白名单自动执行，否则需要人工审批）。"
                ),
                "parameters": query_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_device_query_result",
                "description": f"[{ROOT_TOOL_SCHEMA_VERSION}] 回查一个已提交的设备命令查询提案的当前结果。",
                "parameters": result_parameters,
            },
        },
    ]
```

4. `build_root_tool_dispatcher` 里的 `dispatch()` 加两个分支：

```python
    async def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "query_device_command":
            try:
                parsed = QueryDeviceCommandArgs.model_validate(arguments)
            except ValidationError as exc:
                return ToolResult(control="clarification", content=f"工具 {name!r} 参数无效: {exc.error_count()} 处错误")
            try:
                return await query_device_command(
                    db, session_id=session_id, actor_user_id=actor_user_id,
                    proposed_by_agent_id=proposed_by_agent_id, asset_id=parsed.asset_id,
                    command_name=parsed.command_name, reason=parsed.reason, publisher=publisher,
                )
            except Exception as exc:
                return ToolResult(control="failed", content=f"工具 {name!r} 执行失败: {type(exc).__name__}")
        if name == "get_device_query_result":
            try:
                parsed = GetDeviceQueryResultArgs.model_validate(arguments)
            except ValidationError as exc:
                return ToolResult(control="clarification", content=f"工具 {name!r} 参数无效: {exc.error_count()} 处错误")
            try:
                return await get_device_query_result(db, session_id=session_id, proposal_id=parsed.proposal_id)
            except Exception as exc:
                return ToolResult(control="failed", content=f"工具 {name!r} 执行失败: {type(exc).__name__}")
        if name != "propose_remediation":
            return await read_dispatch(name, arguments)
        # ...原有 propose_remediation 分支不变
```

（这段是往现有 `dispatch()` 函数里插入新分支，不是整个重写；实施时找到现有函数体，在 `if name != "propose_remediation":` 之前插入上面两个 `if` 块。）

修改 `backend/app/agent/chat_turn.py`，`ROOT_OPS_SYSTEM_PROMPT` 追加一段（在 `回答简洁、可操作；涉及风险操作时明确说明需要审批。"""` 前面插入新内容）：

```python
ROOT_OPS_SYSTEM_PROMPT = """你是企业统一运维助手（OpsAssistant）。
你帮助用户做运维知识问答、设备/网段在线状态查询，以及基于 CMDB 的关联排查。
请优先通过已提供的工具取证，再给出有依据的中文回答；不要编造未查到的主机、告警或文档内容。
需要整改或写操作时，只能调用 propose_remediation 提交提案，等待人工审批；
若工具返回等待审批（pending_approval），必须停止并如实告知用户「已提交审批、等待结果」，
禁止杜撰「已执行成功」或伪造执行输出。
需要对某台已在 CMDB 登记凭据的设备做只读诊断（查版本、查配置、查接口、连通性测试）时，
调用 query_device_command；命中白名单会当场执行并把结果直接告诉你，否则会进入人工审批，
此时同样要如实告知用户「已提交审批」，不得编造设备输出；用户后续追问结果时用
get_device_query_result 回查，不确定是否已执行完成就不要编造已经查到的内容。
回答简洁、可操作；涉及风险操作时明确说明需要审批。"""
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_agent_hitl_tools.py tests/test_agent_tool_dispatch.py tests/test_hitl_integration.py -v
uv run mypy app/agent/hitl_tools.py app/agent/tool_dispatch.py app/agent/chat_turn.py
uv run ruff check app/agent/hitl_tools.py app/agent/tool_dispatch.py app/agent/chat_turn.py tests/test_agent_hitl_tools.py
```
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/hitl_tools.py backend/app/agent/tool_dispatch.py backend/app/agent/chat_turn.py backend/tests/test_agent_hitl_tools.py backend/tests/test_agent_tool_dispatch.py
git commit -m "$(cat <<'EOF'
新增 query_device_command / get_device_query_result 两个根 Agent 工具

- query_device_command 包一层 propose_action(action_type=device_query)，
  自动执行时把结果直接塞进 ToolResult.content 让模型当轮就能讲给用户
- get_device_query_result 按 session_id 严格隔离，回查历史提案结果，
  不匹配当成"不存在"，不泄露其它会话的提案存在与否
- 两个工具只挂根 Agent 调度器，子角色 tools_allowlist 不变，跟
  propose_remediation 的收紧方式一致
- system prompt 补充说明：审批中的设备查询要如实告知等待结果，禁止编造
EOF
)"
```

---

### Task 9: `HitlDecideRequest` 支持动态凭据密码

**Files:**
- Modify: `backend/app/schemas/hitl.py`
- Modify: `backend/app/api/v1/hitl.py`
- Test: `backend/tests/test_hitl_api.py`

**Interfaces:**
- `HitlDecideRequest` 加 `dynamic_credential_password: str | None`
- `decide_hitl_proposal` 校验：批准 `device_query` 且资产是动态凭据时该字段必填

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_hitl_api.py` 追加：

```python
async def test_decide_device_query_requires_password_for_dynamic_credential(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: Headers
) -> None:
    # 准备一个 dynamic 凭据资产 + PENDING 的 device_query 提案（复用文件里已有的辅助函数模式）
    ...
    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_decide_device_query_with_password_executes(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ...
    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True, "dynamic_credential_password": "one-time-pass"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "EXECUTED"
    assert "one-time-pass" not in response.text
```

具体资产/提案的准备代码，实施时对照 `test_hitl_api.py` 文件里已有的辅助函数（大概率已经有类似 `_make_pending_proposal` 之类的写法）照抄改造，两个测试都要 `monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", ...)` 且对 `app.agent.executors._open_scrapli_connection` 打桩（参照 Task 7 的做法）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_hitl_api.py -k dynamic -v`
Expected: FAIL（还没有必填校验，或者 422 断言失败因为现在字段还不存在）。

- [ ] **Step 3: 实现**

修改 `backend/app/schemas/hitl.py`：

```python
class HitlDecideRequest(BaseModel):
    """人工审批请求体。"""

    model_config = ConfigDict(extra="forbid")

    approve: bool
    dynamic_credential_password: str | None = Field(default=None, min_length=1, max_length=256)
```

修改 `backend/app/api/v1/hitl.py`，`decide_hitl_proposal` 里，`existing = await hitl_proposal_crud.get(db, proposal_id)` 之后、调用 `decide_proposal` 之前加校验：

```python
    if body.approve and existing.action_type == "device_query":
        raw_asset_id = existing.action_payload.get("asset_id")
        asset = await cmdb_asset_crud.get(db, raw_asset_id) if isinstance(raw_asset_id, int) else None
        if asset is not None and asset.credential_type == "dynamic" and not body.dynamic_credential_password:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="该资产使用动态凭据，批准时必须提供本次登录密码",
            )
```

（需要 import `cmdb_asset_crud`。）`resume_proposal` 调用那一行加上 `dynamic_password=body.dynamic_credential_password`：

```python
        if body.approve:
            await resume_proposal(
                db, proposal_id=proposal_id, actor_user_id=current_user.id,
                publisher=publisher, dynamic_password=body.dynamic_credential_password,
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_hitl_api.py -v
uv run mypy app/schemas/hitl.py app/api/v1/hitl.py
uv run ruff check app/schemas/hitl.py app/api/v1/hitl.py tests/test_hitl_api.py
```
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/schemas/hitl.py backend/app/api/v1/hitl.py backend/tests/test_hitl_api.py
git commit -m "$(cat <<'EOF'
HITL 审批接口支持动态凭据当场输入密码

- HitlDecideRequest 加可选 dynamic_credential_password
- decide_hitl_proposal 批准 device_query 提案时，若目标资产是动态凭据，
  该字段必填，否则 422；这个值只在这一次 HTTP 请求的调用栈里传递到
  resume_proposal → DeviceQueryExecutor，不写进 action_payload、不进审计
EOF
)"
```

---

### Task 10: 策略管理 API

**Files:**
- Create: `backend/app/schemas/device_command_policy.py`
- Create: `backend/app/api/v1/device_command_policies.py`
- Modify: `backend/app/api/router.py`
- Modify: `backend/init_db.py`
- Test: `backend/tests/test_device_command_policy_api.py`

**Interfaces:**
- 8 个端点：list/create/get/update/delete/deleted/restore/purge，`device_command_policy:read`/`device_command_policy:manage` 权限门控

**这个任务结构完全照抄 T08 CMDB 计划里 Task 5（`app/api/v1/cmdb.py`）的模式**（不重复贴一遍完整的 8 个端点代码——实施者对照 `backend/app/api/v1/cmdb.py` 现在的写法，把 `CmdbAsset`/`cmdb_asset_crud` 换成 `DeviceCommandPolicy`/`device_command_policy_crud`，端点前缀从 `/assets` 换成 `/policies`，去掉凭据加解密那部分逻辑（策略表没有密码字段），保留软删除/回收站/审计写法）。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_device_command_policy_api.py`，覆盖：创建 `asset_type` 一条策略成功；创建时 `command_name` 不在目录里报 422；创建重复策略报 409（`DuplicateDeviceCommandPolicyError` 在路由层要局部捕获转成 409，参照 `backend/app/api/v1/hitl.py::decide_hitl_proposal` 已有的写法——那里就是在路由函数体内 `try/except HitlProposalRejectedError` 之类直接捕获转成对应 HTTP 状态码，不是全局 `@app.exception_handler`）；只有 `device_command_policy:read` 权限的账号不能创建（403）；软删除→回收站→恢复→永久删除全流程；创建/修改策略会写审计（`log_audit`，action 比如 `create_device_command_policy`）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_device_command_policy_api.py -v`
Expected: FAIL，404（路由还不存在）。

- [ ] **Step 3: Schema**

创建 `backend/app/schemas/device_command_policy.py`：

```python
"""Device command policy request and response models."""

from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from app.agent.device_commands import list_device_commands
from app.schemas.common import ApiModel

type PolicyScope = Literal["asset_type", "asset"]
type PolicyDecision = Literal["whitelist", "blacklist"]

_VALID_COMMAND_NAMES = frozenset(item.name for item in list_device_commands())


def _validate_command_name(value: str) -> str:
    if value not in _VALID_COMMAND_NAMES:
        raise ValueError(f"未知命令名：{value}")
    return value


class DeviceCommandPolicyCreate(ApiModel):
    """Create a device command policy."""

    scope: PolicyScope
    asset_type: str | None = Field(default=None, max_length=50)
    asset_id: int | None = None
    command_name: str = Field(min_length=1, max_length=100)
    decision: PolicyDecision
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_command_name(self) -> Self:
        _validate_command_name(self.command_name)
        return self

    @model_validator(mode="after")
    def validate_scope_fields(self) -> Self:
        if self.scope == "asset_type":
            if not self.asset_type:
                raise ValueError("scope 为 asset_type 时必须填写 asset_type")
            if self.asset_id is not None:
                raise ValueError("scope 为 asset_type 时不能填写 asset_id")
        else:
            if self.asset_id is None:
                raise ValueError("scope 为 asset 时必须填写 asset_id")
            if self.asset_type is not None:
                raise ValueError("scope 为 asset 时不能填写 asset_type")
        return self


class DeviceCommandPolicyUpdate(ApiModel):
    """Partially update a device command policy (only decision/note are mutable)."""

    decision: PolicyDecision | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def reject_empty_update(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("至少提供一个要更新的字段")
        return self


class DeviceCommandPolicyResponse(ApiModel):
    """Public policy representation."""

    id: int
    scope: PolicyScope
    asset_type: str | None
    asset_id: int | None
    command_name: str
    decision: PolicyDecision
    note: str
    created_by_user_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
```

**`DeviceCommandPolicyUpdate` 只允许改 `decision`/`note`**——`scope`/`asset_type`/`asset_id`/`command_name` 定了就不让改（想改目标就删了重建），避免"改来改去"把一条策略的唯一性校验绕开或者搞出隐藏的状态迁移问题，这是这个任务里一个刻意收窄的设计决定，不是遗漏。

- [ ] **Step 4: API 路由**

创建 `backend/app/api/v1/device_command_policies.py`，结构照抄 `backend/app/api/v1/cmdb.py`（8 个端点，`/policies` 前缀，`device_command_policy:read`/`device_command_policy:manage` 门控，`create`/`update`/`delete`/`restore`/`purge` 都要 `log_audit`，`create`/`update` 捕获 `DuplicateDeviceCommandPolicyError` 转成 409）。`create_policy` 里把 `current_user.id` 塞进 `created_by_user_id`。

修改 `backend/app/api/router.py`：加 import 和注册：

```python
from app.api.v1.device_command_policies import router as device_command_policies_router
...
api_router.include_router(device_command_policies_router, prefix="/device-command-policies", tags=["设备命令策略"])
```

修改 `backend/init_db.py` 的 `SEED_PERMISSIONS`，在 `agent:hitl_approve` 那条前面加：

```python
    {
        "name": "查看设备命令策略",
        "code": "device_command_policy:read",
        "module": "设备命令策略",
        "description": "查看设备命令白/黑名单策略",
    },
    {
        "name": "管理设备命令策略",
        "code": "device_command_policy:manage",
        "module": "设备命令策略",
        "description": "创建/更新/删除设备命令白/黑名单策略",
    },
```

- [ ] **Step 5: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_device_command_policy_api.py -v
uv run pytest -v
uv run mypy app
uv run ruff check .
uv run alembic heads
```
Expected: 全部通过；全量套件零回归；mypy/ruff 干净。

- [ ] **Step 6: 提交**

```bash
git add backend/app/schemas/device_command_policy.py backend/app/api/v1/device_command_policies.py backend/app/api/router.py backend/init_db.py backend/tests/test_device_command_policy_api.py
git commit -m "$(cat <<'EOF'
新增设备命令策略管理 API

- 8 个端点结构照抄 cmdb.py 的 CRUD + 回收站模式；DeviceCommandPolicyUpdate
  只允许改 decision/note，目标（scope/asset_type/asset_id/command_name）
  定了就不让改，想换目标只能删了重建，避免绕开唯一性校验
- command_name 在 schema 层就校验必须命中 app/agent/device_commands.py
  的目录，不接受目录之外的值
- 新增 device_command_policy:read/manage 两条权限码
EOF
)"
```

---

### Task 11: 后端跨组件验收

**Files:**
- Test: `backend/tests/test_device_command_execution_integration.py`

- [ ] **Step 1: 写端到端集成测试**

创建 `backend/tests/test_device_command_execution_integration.py`，至少覆盖：

```python
"""设备命令执行端到端验收：白名单自动执行、黑名单拒绝、动态凭据强制人工、密码零泄露。"""

# test_whitelisted_static_credential_query_executes_in_one_call
#   完整走 query_device_command 工具 → 白名单 → 当场执行 → ToolResult.content 带输出
# test_blacklisted_command_is_rejected_without_creating_proposal
# test_unclassified_command_creates_pending_proposal_visible_via_hitl_api
#   走 query_device_command → PENDING → GET /hitl/proposals 能看到 → POST decide 批准 → 执行完成
# test_dynamic_credential_requires_password_even_when_whitelisted
#   即使命中白名单，decide 时不给 dynamic_credential_password 应该 422；给了才能执行
# test_response_bodies_never_contain_plaintext_or_ciphertext_password
#   全程用一个已知明文密码，断言它不出现在任何一次 HTTP 响应体里
# test_child_agent_dispatcher_rejects_query_device_command
#   跟 test_scenario_c_child_dispatcher_rejects_propose_remediation 一样的写法
```

具体断言参照 Task 5-9 已经各自验证过的分支，这里串成完整链路，不需要重新覆盖已经测过的边界条件。

- [ ] **Step 2: 跑通并做全量验证**

Run:
```bash
uv run pytest tests/test_device_command_execution_integration.py -v
uv run pytest -v
uv run mypy app
uv run ruff check .
uv run alembic heads
```
Expected: 新测试通过；全量套件零回归；mypy/ruff 干净；`alembic heads` 单一 head（`f19a7c3e6d84`）。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_device_command_execution_integration.py
git commit -m "$(cat <<'EOF'
设备命令执行后端端到端验收

- 白名单自动执行、黑名单拒绝不建提案、未分类走 HITL 审批页可见、动态
  凭据即使白名单也强制要密码，四条主链路串联验证
- 断言全程响应体不出现明文/密文密码
- 子 Agent 调度器确认拒绝 query_device_command，写路径仅根 Agent 可走
- 记录全量 pytest/mypy/ruff/alembic heads 验证结果
EOF
)"
```

---

### Task 12: 前端类型 + 策略管理页面

**Files:**
- Create: `frontend/src/types/device-command-policy.ts`
- Create: `frontend/src/components/device-command-policies/DeviceCommandPolicyFormDialog.tsx`
- Create: `frontend/src/pages/DeviceCommandPoliciesPage.tsx`
- Create: `frontend/src/pages/DeviceCommandPoliciesTrashPage.tsx`
- Modify: `frontend/src/lib/constants.ts`

**Interfaces:** 无新导出接口，页面/组件。

- [ ] **Step 1: 类型 + 常量**

创建 `frontend/src/types/device-command-policy.ts`：

```typescript
/** 设备命令策略相关类型 */

export type PolicyScope = "asset_type" | "asset"
export type PolicyDecision = "whitelist" | "blacklist"

export interface DeviceCommandPolicy {
  id: number
  scope: PolicyScope
  asset_type: string | null
  asset_id: number | null
  command_name: string
  decision: PolicyDecision
  note: string
  created_by_user_id: number | null
  created_at: string
  updated_at: string
}

export interface DeviceCommandPolicyCreate {
  scope: PolicyScope
  asset_type?: string | null
  asset_id?: number | null
  command_name: string
  decision: PolicyDecision
  note?: string
}

export interface DeviceCommandPolicyUpdate {
  decision?: PolicyDecision
  note?: string
}

/** 前端展示用命令目录条目（跟后端 app/agent/device_commands.py 手动保持一致） */
export const DEVICE_COMMAND_NAMES = [
  "show_version",
  "show_running_config",
  "show_interfaces",
  "ping",
] as const
```

修改 `frontend/src/lib/constants.ts`：`ROUTES` 加 `DEVICE_COMMAND_POLICIES: "/device-command-policies"`、`DEVICE_COMMAND_POLICIES_TRASH: "/device-command-policies/trash"`；`PERMISSIONS` 加 `DEVICE_COMMAND_POLICY_READ: "device_command_policy:read"`、`DEVICE_COMMAND_POLICY_MANAGE: "device_command_policy:manage"`。

- [ ] **Step 2: 表单对话框**

创建 `frontend/src/components/device-command-policies/DeviceCommandPolicyFormDialog.tsx`，结构参照 `PermissionFormDialog.tsx`：`scope` 用 `Select`（设备类型级别 / 单台设备），选了"设备类型级别"显示 `asset_type` 输入（或下拉，复用 CMDB 表单里的资产类型选项），选了"单台设备"显示一个资产选择器（可以先用简单的 `Input` 输一个资产 ID，v1 不做资产搜索选择器，作为已知的简化项写进任务备注，不阻塞这个任务）；`command_name` 下拉，选项来自 `DEVICE_COMMAND_NAMES`；`decision` 下拉（白名单/黑名单）；`note` 文本域。只支持新增（这份策略"编辑"只改 `decision`/`note`，创建时的 `scope`/目标/命令名字段在编辑态禁用/不显示，参照后端 `DeviceCommandPolicyUpdate` 的收窄决定）。

- [ ] **Step 3: 列表页 + 回收站**

创建 `frontend/src/pages/DeviceCommandPoliciesPage.tsx`（结构照抄 `PermissionsPage.tsx`：`DataTable` + 新增/编辑/删除 + 回收站入口，列：目标（`scope=asset_type` 显示"类型：{asset_type}"，`scope=asset` 显示"资产 #{asset_id}"）、命令名、决定（Badge 区分白/黑名单颜色）、备注、创建时间）。

创建 `frontend/src/pages/DeviceCommandPoliciesTrashPage.tsx`（结构照抄 `CmdbAssetsTrashPage.tsx`）。

- [ ] **Step 4: 验证**

Run（从 `frontend/`）:
```bash
npm run typecheck
npm run lint
npm run test
```
Expected: 干净。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/types/device-command-policy.ts frontend/src/components/device-command-policies frontend/src/pages/DeviceCommandPoliciesPage.tsx frontend/src/pages/DeviceCommandPoliciesTrashPage.tsx frontend/src/lib/constants.ts
git commit -m "$(cat <<'EOF'
新增设备命令策略管理页面（含回收站）

- 结构照抄 PermissionsPage.tsx/CmdbAssetsTrashPage.tsx，command_name 下拉
  而不是自由输入，跟后端目录保持一致
- 编辑态只能改 decision/note，目标字段创建后不可变，对齐后端
  DeviceCommandPolicyUpdate 的收窄设计
- v1 单台设备目标用资产 ID 输入框，不做资产搜索选择器，留作已知简化项
EOF
)"
```

---

### Task 13: `HitlApprovalCard` 展示结果 + 动态凭据密码输入

**Files:**
- Modify: `frontend/src/components/ops-assistant/HitlApprovalCard.tsx`
- Modify: `frontend/src/lib/hitl-api.ts`
- Test: `frontend/src/components/ops-assistant/HitlApprovalCard.test.tsx`（如果目前没有这个文件就新建；如果 `decideHitlProposal` 之类的函数已有测试就在旁边追加）

**Interfaces:**
- `decideHitlProposal(id, { approve, dynamic_credential_password? })`
- 卡片新增：`EXECUTED` 状态下展示 `result_excerpt`；`action_type === "device_query"` 且批准时资产是动态凭据，批准按钮旁多一个密码输入框

- [ ] **Step 1: 确认现状**

Run（从 `frontend/`）: `cat src/lib/hitl-api.ts`（或者用 Read 工具看），确认 `decideHitlProposal`/`HitlProposal` 类型现在的精确签名，因为要往请求体和类型定义里加一个可选字段。

- [ ] **Step 2: 类型 + API**

修改 `frontend/src/lib/hitl-api.ts`：`HitlProposal` 类型如果目前没有 `result_excerpt`/资产凭据信息，加 `result_excerpt?: string | null`；`decideHitlProposal` 的请求体类型加 `dynamic_credential_password?: string`。

- [ ] **Step 3: 卡片组件**

修改 `frontend/src/components/ops-assistant/HitlApprovalCard.tsx`：

1. `displayStatus === "EXECUTED"` 且 `detail?.result_excerpt`（或 WS 摘要里带的 `result_excerpt`）非空时，`CardContent` 里加一段等宽字体的展示区（参照现有"完整 payload"展示区 `<pre>` 的样式）。
2. 判断"当前提案是否需要在批准时输入动态密码"：`displayActionType === "device_query"` 且 `detail`（完整 payload，只有 `canApprove` 才会拉到）里能看出资产是动态凭据——**这里需要后端在 `HitlProposalResponse` 里透出资产的 `credential_type`，实施时确认一下现在的 `HitlProposalResponse`/`_to_response` 有没有带这个信息；如果没有，这个任务要连带给 `app/schemas/hitl.py::HitlProposalResponse` 和 `app/api/v1/hitl.py::_to_response`（或等价函数）加一个 `asset_credential_type: str | None` 字段，从 `cmdb_asset_crud.get(asset_id)` 查出来附加上去（只有审批人能看到完整 payload，这个信息暴露给审批人是合理的，不违反"密码不落库不进审计"的约束，因为这只是凭据类型不是密码本身）**。
3. `canApprove && isPending && displayActionType === "device_query" && needsDynamicPassword` 时，`CardFooter` 的批准按钮旁边加一个受控 `Input type="password"`，`handleApprove` 里把这个值传给 `decideHitlProposal(proposalId, { approve: true, dynamic_credential_password: password })`；密码框留空时点批准要禁用按钮（前端也做一次必填校验，跟后端的 422 呼应，减少一次无意义的失败请求往返）。

- [ ] **Step 4: 测试**

在 `frontend/src/components/ops-assistant/HitlApprovalCard.test.tsx`（新建或追加）覆盖：`result_excerpt` 有值时渲染出来；`device_query` + 动态凭据时批准按钮在密码为空时禁用。测试范围参照项目里其它组件测试目前的深度（如果这个组件目前完全没有测试文件，这一步先补最基础的渲染/交互测试，不需要覆盖到跟 T11 验收测试同等深度）。

- [ ] **Step 5: 验证**

Run（从 `frontend/`）:
```bash
npm run typecheck
npm run lint
npm run test
```
Expected: 干净。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/components/ops-assistant/HitlApprovalCard.tsx frontend/src/lib/hitl-api.ts frontend/src/components/ops-assistant/HitlApprovalCard.test.tsx
git commit -m "$(cat <<'EOF'
HitlApprovalCard 展示设备命令执行结果，动态凭据批准时可当场输密码

- EXECUTED 状态展示 result_excerpt（等宽字体，参照现有 payload 展示区样式）
- device_query 提案且目标资产是动态凭据时，批准按钮旁加密码输入框，
  留空禁用批准按钮，跟后端 422 校验呼应
- HitlProposalResponse 透出资产 credential_type，供前端判断要不要显示
  密码框；这是凭据类型不是密码本身，只对有审批权限的人可见，不违反
  密码零落库/零审计的约束
EOF
)"
```

---

### Task 14: 侧栏菜单 + 路由 + 前后端端到端验收

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/layout/Sidebar.tsx`

- [ ] **Step 1: 注册路由**

修改 `frontend/src/App.tsx`：import 加 `DeviceCommandPoliciesPage`/`DeviceCommandPoliciesTrashPage`，在"运维管理"分组相关路由（Task CMDB 计划已经建立的 `/cmdb`、`/cmdb/trash`）附近加两条 `<Route>`，权限分别是 `PERMISSIONS.DEVICE_COMMAND_POLICY_READ`/`DEVICE_COMMAND_POLICY_MANAGE`。

- [ ] **Step 2: 菜单**

修改 `frontend/src/components/layout/Sidebar.tsx`：`NAV_ENTRIES` 里"运维管理"分组的 `children` 加一条"设备命令策略"，`permission: PERMISSIONS.DEVICE_COMMAND_POLICY_READ`。

- [ ] **Step 3: 验证**

Run（从 `frontend/`）:
```bash
npm run typecheck
npm run lint
npm run test
```
Expected: 干净。

- [ ] **Step 4: 端到端手动验收**

参照本项目一贯的收尾方式（T11/CMDB 凭据管理最后一次验收提交），启动前后端，走一遍完整链路：

1. `uv run python main.py`（backend/）+ `npm run dev`（frontend/），登录一个有 `cmdb:manage`/`device_command_policy:manage`/`agent:hitl_approve` 的账号。
2. CMDB 资产管理里编辑一台资产，填 `vendor=cisco_iosxe`、`credential_type=static` + 一个测试密码，保存。
3. 设备命令策略页面新增一条 `scope=asset`、目标是这台资产、`command_name=show_version`、`decision=whitelist`。
4. 运维助手对话里问"帮我查一下这台设备的版本"（提供资产 IP/主机名），确认 Agent 调用 `query_device_command` 并且（如果有可连通的测试设备）当场拿到输出；没有真实测试设备的话，这一步至少确认走到"尝试连接失败但不泄露原始异常"这个分支，Network 面板确认响应体里没有测试密码明文。
5. 把这条策略的 `decision` 改成没有策略（删掉），再问一次，确认审批卡片出现在聊天消息流里，`agent:hitl_approve` 账号能看到并批准；如果资产是动态凭据，确认批准框里能输密码且留空点不了批准。
6. 用一个只有 `cmdb:read` 没有 `device_command_policy:manage` 的账号，确认策略管理页面的"新增"按钮不出现，直接调用创建 API 返回 403。

把关键截图或终端输出记录在这一步的 commit message 里。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/App.tsx frontend/src/components/layout/Sidebar.tsx
git commit -m "$(cat <<'EOF'
设备命令执行能力接入侧栏导航，完成前后端端到端验收

- 新增两条路由（策略管理 + 回收站），挂进"运维管理"分组
- 手动走完：资产配凭据 → 建白名单策略 → 对话里查询自动执行 → 删策略后
  走审批卡片 → 动态凭据当场输密码 → 无权限账号 403 的完整链路，
  Network 面板确认全程响应体不含明文密码
EOF
)"
```

---

## After All Tasks

- 用 `superpowers:verification-before-completion` 报告一次新鲜的命令输出：后端 `uv run pytest -v && uv run mypy app && uv run ruff check . && uv run alembic heads`；前端 `npm run typecheck && npm run lint && npm run test`。
- 派一个 `superpowers:requesting-code-review` 走一遍全分支评审，重点检查：密码（静态解密后的明文、动态当场输入的明文）是否在任何路径（响应体/日志/审计 detail/`action_payload`/异常消息）泄露过；动态凭据 + 白名单这个组合是否真的被测试锁死、代码里没有第二条能绕过人工审批的路径；`resolve_policy` 的优先级测试是否覆盖了两个方向；黑名单是否真的不创建 `PENDING` 记录；`DeviceCommandPolicyUpdate` 是否真的不能改目标字段。
- 确认 `git status --short` 干净。全程留在 `master`，不建分支、不建 PR，不主动 push。
- 不要在 Tasks 1-14 未全部完成、验收矩阵未全绿之前，宣称这个功能已经完成。
