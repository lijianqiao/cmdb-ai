# 系统运行配置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 8 个 LLM/Embedding 参数和 4 个 HITL/监控参数纳入数据库驱动的“系统配置”，提供严格 RBAC 保护的前端配置页，并让修改后的值在后续模型调用、HITL 提案和后台巡检周期中生效。

**Architecture:** 新增逐键存储的 `system_configs` 表，所有配置键和类型由代码白名单控制，数据库值优先于现有 `.env` 回退值。LLM API Key 复用现有 `CMDB_CREDENTIAL_KEY` 做 Fernet 加密后入库且永不通过 API 回显；运行时不做进程缓存，而是在每次模型/Embedding 调用、HITL 提案或后台任务新一轮开始时读取配置，从而避免多 worker 缓存不一致。

**Tech Stack:** Python 3.14.3、FastAPI、Pydantic v2、SQLAlchemy 2 async、Alembic、PostgreSQL、cryptography/Fernet、React 19、TypeScript 6、React Hook Form、Zod、shadcn/Base UI、Vitest、pytest。

## Global Constraints

- 只在 `master` 分支工作，不创建分支或 PR；每个任务完成并验证后按项目规范提交一次。
- 后端命令一律在 `backend/` 下使用 `uv run`；前端命令在 `frontend/` 下使用现有 `pnpm`，不新增依赖。
- 不编辑或提交被忽略的 `backend/.env`；只更新 `backend/.env.example` 和文档。
- 受管配置键严格限定为以下 12 个，不在本次范围加入通用“任意 key/value 配置编辑器”：
  - `LLM_CHAT_BASE_URL`
  - `LLM_CHAT_API_KEY`
  - `LLM_CHAT_MODEL`
  - `LLM_CHAT_INPUT_COST_PER_MILLION_USD`
  - `LLM_CHAT_OUTPUT_COST_PER_MILLION_USD`
  - `LLM_EMBEDDING_BASE_URL`
  - `LLM_EMBEDDING_API_KEY`
  - `LLM_EMBEDDING_MODEL`
  - `HITL_NOTIFY_AUTO_APPROVE`
  - `MONITOR_PROBE_TIMEOUT_SECONDS`
  - `MONITOR_SWEEP_INTERVAL_SECONDS`
  - `CMDB_DIFF_INTERVAL_SECONDS`
- `LLM_EMBEDDING_API_KEY` 当前可能未出现在本机 `.env`，但它已存在于 `Settings`，并计入用户要求的 8 个模型配置项。
- 数据库覆盖值优先；对应数据库行不存在时沿用当前 `Settings`/`.env` 值，保证升级时服务不会突然失效。
- `init_db.py` 不创建任何 LLM/Embedding 配置行；只幂等创建 4 个 HITL/监控配置行，初始值取初始化进程当前解析到的 `Settings` 值，因此未配置环境变量时分别为 `false`、`3.0`、`30.0`、`3600.0`。
- 使用一个权限码 `system_config:manage` 同时保护页面读取与更新；超级管理员继续通过现有 `require_permission()` 和 `ProtectedRoute` 机制自动放行。
- 现有 `CMDB_CREDENTIAL_KEY` 作为数据库可逆秘密值的统一 Fernet 根密钥，同时保护 CMDB 静态设备密码和 LLM/Embedding API Key；不得与 JWT `SECRET_KEY` 混用。
- 共享密钥意味着泄露会同时暴露两类秘密值，丢失会同时导致两类密文不可解；轮换时必须在同一维护窗口重新加密 `cmdb_assets.credential_password_encrypted` 和 `system_configs` 中两个 API Key，不能只替换 `.env` 值。
- API Key 请求字段使用 `SecretStr`，数据库只存 Fernet 密文；GET/PUT 响应、审计日志、应用日志和异常信息均不得包含明文或密文。
- 页面不增加“测试连接”按钮；自动化测试只使用 `httpx.MockTransport`，实施和验收期间不产生真实 LLM/Embedding 请求或费用。
- 配置生效语义固定为：LLM/Embedding 在下一次调用生效，HITL 在下一次提案生效，探测超时在下一轮 sweep 生效，两个任务间隔在当前 sleep 结束后的下一轮重新读取；不重启进程，也不强行中断正在执行的请求或 sleep。
- 所有写 API 遵守现有事务约定：业务变更与审计记录在同一个 `AsyncSession` 中 flush，路由层只 commit 一次，异常由 `get_db()` 回滚。
- Commit 第一行写清改动，空一行后列出“改了什么 + 为什么”；禁止 `Co-Authored-By`。

---

## Scope and Design Decisions

这 12 个值虽然由四条运行链路消费，但它们共享同一个持久化、权限、审计和 UI 边界，因此使用一个计划、按可独立验收的任务拆分，不再拆成四份计划。

### 配置来源优先级

```text
数据库存在该 key 的行
    ├─ value 有值：使用数据库值（API Key 先解密）
    └─ value 为 NULL：仅允许两个 API Key，表示管理员明确清空，不再回退 .env
数据库不存在该 key 的行
    └─ 使用 Settings/.env 兼容值
```

### API 契约

```text
GET /api/v1/system-config
PUT /api/v1/system-config/llm
PUT /api/v1/system-config/operations

三条接口全部要求 system_config:manage；超级管理员自动放行。
```

`GET` 返回有效配置，但两个 API Key 只返回 `configured` 与 `source`：

```json
{
  "code": 200,
  "data": {
    "llm": {
      "chat_base_url": "http://127.0.0.1:8080/v1",
      "chat_model": "local-chat",
      "chat_input_cost_per_million_usd": 0.0,
      "chat_output_cost_per_million_usd": 0.0,
      "chat_api_key_configured": false,
      "chat_api_key_source": "unset",
      "embedding_base_url": "http://127.0.0.1:8080/v1",
      "embedding_model": "Qwen3-Embedding-0.6B",
      "embedding_api_key_configured": false,
      "embedding_api_key_source": "unset"
    },
    "operations": {
      "hitl_notify_auto_approve": false,
      "monitor_probe_timeout_seconds": 3.0,
      "monitor_sweep_interval_seconds": 30.0,
      "cmdb_diff_interval_seconds": 3600.0
    }
  },
  "message": "success"
}
```

`source` 的类型固定为 `"database" | "environment" | "unset"`。PUT LLM 时，API Key 字段未出现表示保留，提供非空值表示替换，`clear_*_api_key=true` 表示写入数据库 `NULL` 并显式覆盖环境变量；同一次请求禁止“提供新 Key”和“清空 Key”。

## File Structure

### Backend files to create

- `backend/app/models/system_config.py`：一行一个配置键的 ORM 模型。
- `backend/app/crud/system_config.py`：按键批量读取、幂等补缺和事务内 upsert。
- `backend/app/core/data_encryption.py`：使用现有 `CMDB_CREDENTIAL_KEY` 提供通用 Fernet 加解密，不绑定具体业务字段。
- `backend/app/schemas/system_config.py`：LLM/运行参数请求、响应、URL/范围/清空语义校验。
- `backend/app/services/system_config.py`：键白名单、序列化、数据库优先级、有效配置快照和更新服务。
- `backend/app/api/v1/system_config.py`：三条受权限保护的配置 API 和审计。
- `backend/alembic/versions/2026_08_13_1400-a2f6c8d91e37_system_configs.py`：创建 `system_configs` 表，不插入业务值。
- `backend/tests/test_system_config_model.py`：模型与键唯一性。
- `backend/tests/test_system_config_crud.py`：批量读取/upsert/create-missing。
- `backend/tests/test_data_encryption.py`：共享密钥的密文、缺钥匙、错钥匙行为。
- `backend/tests/test_system_config_service.py`：来源优先级、类型解析、秘密清空。
- `backend/tests/test_system_config_api.py`：RBAC、响应脱敏、审计、校验和事务。
- `backend/tests/test_system_config_seeds.py`：权限种子和 4 个运行参数种子契约。

### Backend files to modify

- `backend/app/core/config.py:46-170`：保留 12 个兼容回退项，并将现有 `CMDB_CREDENTIAL_KEY` 的注释明确为共享数据库秘密加密密钥。
- `backend/app/core/cmdb_credential.py`：保留原公共接口，改为调用通用加密模块，确保已有 CMDB 调用方不需要迁移 import。
- `backend/app/core/llm.py:21-57,260-379`：在传入数据库会话时合并数据库模型配置。
- `backend/app/agent/loop.py:56-84`：默认 `chat` 路径传递当前 `AsyncSession`。
- `backend/app/agent/chat_turn.py:94-133`：默认流式 chat 传递当前会话，注入 mock 保持原契约。
- `backend/app/agent/knowledge_tools.py:96-109`：语义检索 embedding 使用数据库配置。
- `backend/app/services/knowledge_ingestion.py:70-112`：文档入库 embedding 使用数据库配置。
- `backend/app/agent/hitl.py:286-307`：notify 自动审批读取有效运行配置。
- `backend/app/services/monitor_sweep.py:44-84`：每轮读取探测超时和 sweep 间隔。
- `backend/app/services/cmdb_diff.py:79-96`：每轮读取差异巡检间隔。
- `backend/app/models/__init__.py`、`backend/app/crud/__init__.py`：导出新模型/CRUD。
- `backend/app/api/router.py`：注册 `/system-config`。
- `backend/init_db.py`：新增权限定义和 4 个运行配置的幂等种子，不创建 LLM 行。
- `backend/.env.example`：解释数据库优先级，并说明现有 `CMDB_CREDENTIAL_KEY` 同时保护两类数据库秘密。
- `backend/tests/conftest.py`：固定测试用 `CMDB_CREDENTIAL_KEY`，避免读取开发者 `.env`。
- `backend/tests/test_cmdb_credential.py`：验证原 CMDB 加解密接口在通用模块之上保持兼容。
- `backend/tests/test_agent_llm.py`、`backend/tests/test_agent_hitl.py`、`backend/tests/test_monitor_sweep.py`、`backend/tests/test_cmdb_diff.py`：覆盖数据库动态值已接入真实消费点。

### Frontend files to create

- `frontend/src/types/system-config.ts`：API 响应和更新载荷类型。
- `frontend/src/lib/system-config-api.ts`：三条配置 API 的类型安全封装。
- `frontend/src/components/system-config/systemConfigFormSchemas.ts`：Zod 表单规则与 LLM 更新载荷构造。
- `frontend/src/components/system-config/systemConfigFormSchemas.test.ts`：URL、数值范围和 Key 保留/替换/清空测试。
- `frontend/src/components/system-config/LlmConfigCard.tsx`：Chat/Embedding 配置卡片。
- `frontend/src/components/system-config/OperationsConfigCard.tsx`：HITL/监控配置卡片与说明文案。
- `frontend/src/components/system-config/OperationsConfigCard.test.tsx`：四项说明和边界提示的渲染测试。
- `frontend/src/pages/SystemConfigPage.tsx`：加载、错误、刷新及两个配置卡片编排。

### Frontend files to modify

- `frontend/src/lib/constants.ts`：增加路由和 `system_config:manage`。
- `frontend/src/App.tsx`：新增受权限保护的系统配置路由。
- `frontend/src/components/layout/Sidebar.tsx`：在“系统管理”组加入入口。
- `frontend/src/components/layout/Header.tsx`：加入正确面包屑，避免显示“页面”。
- `frontend/src/pages/AuditLogsPage.tsx`：加入两类配置更新和配置种子的中文动作名。

### Documentation files to create/modify

- Create `docs/SYSTEM_CONFIG.md`：来源优先级、种子、安全、生效时机和部署步骤。
- Modify `README_zh.md`：增加系统配置入口和文档链接，不重写已有 RBAC 说明。

---

### Task 1: Persist only allowlisted system-config rows

**Files:**
- Create: `backend/app/models/system_config.py`
- Create: `backend/app/crud/system_config.py`
- Create: `backend/alembic/versions/2026_08_13_1400-a2f6c8d91e37_system_configs.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/app/crud/__init__.py`
- Test: `backend/tests/test_system_config_model.py`
- Test: `backend/tests/test_system_config_crud.py`

**Interfaces:**
- Consumes: `Base`, `TimestampMixin`, caller-owned `AsyncSession` and the existing “CRUD flushes, service/API commits” transaction convention.
- Produces: `SystemConfig`; `system_config_crud.get_by_keys()`、`upsert_values()`、`create_missing()`，供服务层和 `init_db.py` 使用。

- [ ] **Step 1: Write failing model and CRUD tests**

```python
"""SystemConfig persistence contract."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.system_config import system_config_crud
from app.models.system_config import SystemConfig

pytestmark = pytest.mark.asyncio


async def test_create_missing_is_idempotent_and_never_overwrites(
    db_session: AsyncSession,
) -> None:
    created = await system_config_crud.create_missing(
        db_session,
        {"MONITOR_SWEEP_INTERVAL_SECONDS": "30.0"},
        updated_by_user_id=None,
    )
    await db_session.commit()
    assert created == 1

    row = (
        await db_session.execute(
            select(SystemConfig).where(
                SystemConfig.key == "MONITOR_SWEEP_INTERVAL_SECONDS"
            )
        )
    ).scalar_one()
    row.value = "45.0"
    await db_session.commit()

    created_again = await system_config_crud.create_missing(
        db_session,
        {"MONITOR_SWEEP_INTERVAL_SECONDS": "30.0"},
        updated_by_user_id=None,
    )
    await db_session.commit()
    assert created_again == 0
    assert row.value == "45.0"


async def test_upsert_supports_explicit_null_for_secret_override(
    db_session: AsyncSession,
) -> None:
    rows = await system_config_crud.upsert_values(
        db_session,
        {"LLM_CHAT_API_KEY": None},
        updated_by_user_id=None,
    )
    await db_session.commit()

    assert rows["LLM_CHAT_API_KEY"].value is None
    assert (await system_config_crud.get_by_keys(
        db_session, ["LLM_CHAT_API_KEY"]
    ))["LLM_CHAT_API_KEY"].value is None
```

`test_system_config_model.py` 还要断言：`key` 唯一且非空、`value` 可空、`updated_by_user_id` 可空并指向 `users.id`。

- [ ] **Step 2: Run the focused tests and verify the imports fail**

Run from `backend/`:

```powershell
uv run pytest tests/test_system_config_model.py tests/test_system_config_crud.py -v
```

Expected: collection fails because `app.models.system_config` and `app.crud.system_config` do not exist.

- [ ] **Step 3: Implement the model and CRUD contract**

```python
class SystemConfig(Base, TimestampMixin):
    __tablename__ = "system_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
```

`CRUDSystemConfig` 必须使用一次批量 SELECT 获取已有键，再逐个更新或新增并 `flush()`；`create_missing()` 只能补不存在的键，不能覆盖已有值。所有方法均不调用 `commit()`。

```python
class CRUDSystemConfig:
    async def get_by_keys(
        self,
        db: AsyncSession,
        keys: Collection[str],
    ) -> dict[str, SystemConfig]:
        normalized = tuple(dict.fromkeys(keys))
        if not normalized:
            return {}
        result = await db.execute(
            select(SystemConfig).where(SystemConfig.key.in_(normalized))
        )
        return {row.key: row for row in result.scalars().all()}

    async def upsert_values(
        self,
        db: AsyncSession,
        values: Mapping[str, str | None],
        *,
        updated_by_user_id: int | None,
    ) -> dict[str, SystemConfig]:
        rows = await self.get_by_keys(db, values.keys())
        for key, value in values.items():
            row = rows.get(key)
            if row is None:
                row = SystemConfig(
                    key=key,
                    value=value,
                    updated_by_user_id=updated_by_user_id,
                )
                db.add(row)
                rows[key] = row
            else:
                row.value = value
                row.updated_by_user_id = updated_by_user_id
        await db.flush()
        return rows
```

- [ ] **Step 4: Add the Alembic migration and exports**

The migration must declare `revision = "a2f6c8d91e37"` and `down_revision = "f19a7c3e6d84"`, create the four columns above plus timestamps, create the unique key index, and use the repository’s `_require_destructive_downgrade()` guard before dropping the table. It must not insert any configuration values.

```python
op.create_table(
    "system_configs",
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("key", sa.String(length=100), nullable=False),
    sa.Column("value", sa.Text(), nullable=True),
    sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
    sa.ForeignKeyConstraint(
        ["updated_by_user_id"], ["users.id"], ondelete="SET NULL"
    ),
)
op.create_index(
    "ix_system_configs_key", "system_configs", ["key"], unique=True
)
```

- [ ] **Step 5: Run focused tests and static checks**

```powershell
uv run pytest tests/test_system_config_model.py tests/test_system_config_crud.py -v
uv run ruff check app/models/system_config.py app/crud/system_config.py tests/test_system_config_model.py tests/test_system_config_crud.py
uv run mypy app/models/system_config.py app/crud/system_config.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add backend/app/models/system_config.py backend/app/crud/system_config.py backend/app/models/__init__.py backend/app/crud/__init__.py backend/alembic/versions/2026_08_13_1400-a2f6c8d91e37_system_configs.py backend/tests/test_system_config_model.py backend/tests/test_system_config_crud.py
git commit -m "新增系统配置持久化模型" -m "- 新增 system_configs 表、ORM 模型与唯一配置键约束
- 提供批量读取、幂等补缺和事务内 upsert，为配置服务与种子初始化提供稳定接口
- 保持 CRUD 只 flush、调用层统一 commit 的现有事务边界"
```

---

### Task 2: Add typed config resolution and shared-key secret storage

**Files:**
- Create: `backend/app/core/data_encryption.py`
- Create: `backend/app/schemas/system_config.py`
- Create: `backend/app/services/system_config.py`
- Modify: `backend/app/core/config.py:84-170`
- Modify: `backend/app/core/cmdb_credential.py`
- Modify: `backend/.env.example`
- Modify: `backend/tests/conftest.py:20-36`
- Test: `backend/tests/test_data_encryption.py`
- Test: `backend/tests/test_system_config_service.py`
- Modify test: `backend/tests/test_cmdb_credential.py`

**Interfaces:**
- Consumes: `system_config_crud` from Task 1, the existing `Settings` fields as compatibility fallbacks, and the already-deployed `CMDB_CREDENTIAL_KEY` Fernet key.
- Produces: `encrypt_secret()`、`decrypt_secret()`；backward-compatible `encrypt_credential_password()`、`decrypt_credential_password()`；`EffectiveLlmConfig`, `EffectiveOperationsConfig`, `get_effective_llm_config()`、`get_effective_operations_config()`、`save_llm_config()`、`save_operations_config()` and `build_system_config_response()`.

- [ ] **Step 1: Write failing crypto, validation and source-precedence tests**

```python
async def test_database_value_overrides_environment_fallback(
    db_session: AsyncSession,
) -> None:
    await system_config_crud.upsert_values(
        db_session,
        {
            "LLM_CHAT_BASE_URL": "https://db-llm.example/v1",
            "LLM_CHAT_MODEL": "db-chat-model",
        },
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)
    assert config.chat_base_url == "https://db-llm.example/v1"
    assert config.chat_model == "db-chat-model"


async def test_explicit_null_api_key_blocks_environment_fallback(
    db_session: AsyncSession,
) -> None:
    await system_config_crud.upsert_values(
        db_session,
        {"LLM_CHAT_API_KEY": None},
        updated_by_user_id=None,
    )
    config = await get_effective_llm_config(db_session)
    assert config.chat_api_key == ""
    assert config.chat_api_key_source == "database"
    assert config.chat_api_key_configured is False


def test_shared_data_encryption_round_trip_without_plaintext_in_ciphertext() -> None:
    ciphertext = encrypt_secret("sk-sensitive-value")
    assert "sk-sensitive-value" not in ciphertext
    assert decrypt_secret(ciphertext) == "sk-sensitive-value"


def test_cmdb_wrapper_and_generic_crypto_use_the_same_key() -> None:
    cmdb_ciphertext = encrypt_credential_password("device-password")
    generic_ciphertext = encrypt_secret("llm-api-key")

    assert decrypt_secret(cmdb_ciphertext) == "device-password"
    assert decrypt_credential_password(generic_ciphertext) == "llm-api-key"


def test_existing_fernet_cmdb_ciphertext_needs_no_data_migration() -> None:
    key = settings.CMDB_CREDENTIAL_KEY
    assert key is not None
    legacy_ciphertext = Fernet(
        key.get_secret_value().encode("utf-8")
    ).encrypt(b"existing-device-password").decode("utf-8")

    assert (
        decrypt_credential_password(legacy_ciphertext)
        == "existing-device-password"
    )
```

Add schema parameterized tests for these exact invalid values: `ftp://host/v1`、a URL containing `user:password@host`、negative/NaN/infinite prices、timeout `0`/`31`、sweep interval `4`/`3601`、diff interval `59`/`86401`、and simultaneous `chat_api_key` plus `clear_chat_api_key=true`.

- [ ] **Step 2: Run tests to verify missing modules fail**

```powershell
uv run pytest tests/test_data_encryption.py tests/test_system_config_service.py tests/test_cmdb_credential.py -v
```

Expected: collection fails because `app.core.data_encryption` and the system-config service do not exist.

- [ ] **Step 3: Generalize the existing CMDB encryption key without renaming it**

Do not add a second encryption setting and do not rename the deployed variable in this feature. Keep the existing validated field:

```python
CMDB_CREDENTIAL_KEY: SecretStr | None = None
```

Change its nearby comments in `config.py` and `.env.example` to state that the legacy name now represents the shared key for reversible database secrets:

```dotenv
# 数据库可逆秘密值的共享 Fernet 密钥：同时保护 CMDB 静态密码和 LLM API Key。
# 泄露、丢失或轮换会同时影响两类密文；必须稳定备份，禁止与 JWT SECRET_KEY 混用。
# 在 backend/ 下生成：
# uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CMDB_CREDENTIAL_KEY=
```

Keep the existing LLM/Embedding and monitor variables in `.env.example`, but label them “数据库未配置该键时的启动兼容回退”. Add the currently missing fallback line `HITL_NOTIFY_AUTO_APPROVE=false`; do not add it to the eight-key LLM seed set because that set must remain empty.

The test fixture must pin the existing key before importing the application so the suite never inherits a developer key:

```python
os.environ["CMDB_CREDENTIAL_KEY"] = (
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
)
```

- [ ] **Step 4: Extract generic crypto and preserve the CMDB public interface**

`data_encryption.py` is the only module that constructs `Fernet`. It reads `settings.CMDB_CREDENTIAL_KEY` and exposes business-neutral functions:

```python
class DataEncryptionKeyMissingError(RuntimeError):
    """共享数据库加密密钥未配置。"""


class DataDecryptError(RuntimeError):
    """数据库密文损坏或共享密钥不匹配。"""


def _fernet() -> Fernet:
    key = settings.CMDB_CREDENTIAL_KEY
    if key is None or not key.get_secret_value().strip():
        raise DataEncryptionKeyMissingError(
            "CMDB_CREDENTIAL_KEY 未配置，无法保存或读取数据库秘密值"
        )
    return Fernet(key.get_secret_value().encode("utf-8"))


def encrypt_secret(plain_value: str) -> str:
    return _fernet().encrypt(plain_value.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise DataDecryptError("数据库密文无法解密，共享密钥可能已更换") from exc
```

`cmdb_credential.py` retains its existing names and translates the generic exceptions so current imports and API error handling remain stable:

```python
class CmdbCredentialKeyMissingError(DataEncryptionKeyMissingError):
    """CMDB 凭据缺少共享数据库加密密钥。"""


class CmdbCredentialDecryptError(DataDecryptError):
    """CMDB 凭据密文无法解密。"""


def encrypt_credential_password(plain_password: str) -> str:
    try:
        return encrypt_secret(plain_password)
    except DataEncryptionKeyMissingError as exc:
        raise CmdbCredentialKeyMissingError(str(exc)) from exc


def decrypt_credential_password(ciphertext: str) -> str:
    try:
        return decrypt_secret(ciphertext)
    except DataEncryptionKeyMissingError as exc:
        raise CmdbCredentialKeyMissingError(str(exc)) from exc
    except DataDecryptError as exc:
        raise CmdbCredentialDecryptError(str(exc)) from exc
```

The system-config service imports `encrypt_secret()`/`decrypt_secret()` directly. Error messages may name `CMDB_CREDENTIAL_KEY` but must never contain its value, plaintext secrets or ciphertext.

- [ ] **Step 5: Implement strict request/response schemas**

```python
type ConfigValueSource = Literal["database", "environment", "unset"]


class LlmSystemConfigUpdate(ApiModel):
    chat_base_url: str = Field(min_length=1, max_length=2048)
    chat_api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_chat_api_key: bool = False
    chat_model: str = Field(min_length=1, max_length=200)
    chat_input_cost_per_million_usd: float = Field(ge=0, allow_inf_nan=False)
    chat_output_cost_per_million_usd: float = Field(ge=0, allow_inf_nan=False)
    embedding_base_url: str = Field(min_length=1, max_length=2048)
    embedding_api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_embedding_api_key: bool = False
    embedding_model: str = Field(min_length=1, max_length=200)


class OperationsSystemConfigUpdate(ApiModel):
    hitl_notify_auto_approve: bool
    monitor_probe_timeout_seconds: float = Field(gt=0, le=30, allow_inf_nan=False)
    monitor_sweep_interval_seconds: float = Field(ge=5, le=3600, allow_inf_nan=False)
    cmdb_diff_interval_seconds: float = Field(ge=60, le=86_400, allow_inf_nan=False)


class LlmSystemConfigResponse(ApiModel):
    chat_base_url: str
    chat_model: str
    chat_input_cost_per_million_usd: float
    chat_output_cost_per_million_usd: float
    chat_api_key_configured: bool
    chat_api_key_source: ConfigValueSource
    embedding_base_url: str
    embedding_model: str
    embedding_api_key_configured: bool
    embedding_api_key_source: ConfigValueSource


class OperationsSystemConfigResponse(ApiModel):
    hitl_notify_auto_approve: bool
    monitor_probe_timeout_seconds: float
    monitor_sweep_interval_seconds: float
    cmdb_diff_interval_seconds: float


class SystemConfigResponse(ApiModel):
    llm: LlmSystemConfigResponse
    operations: OperationsSystemConfigResponse
```

Base-URL validation must accept only `http`/`https`, require a hostname, reject username/password/query/fragment, trim whitespace and remove a trailing slash. The model validator must reject a new key together with its matching clear flag.

- [ ] **Step 6: Implement the allowlist and effective-value service**

Define all 12 keys as constants and group them into immutable tuples. Do not accept a key supplied by the HTTP client; API payload field names map to constants inside the service.

```python
@dataclass(frozen=True, slots=True)
class EffectiveLlmConfig:
    chat_base_url: str
    chat_api_key: str
    chat_api_key_source: ConfigValueSource
    chat_model: str
    chat_input_cost_per_million_usd: float
    chat_output_cost_per_million_usd: float
    embedding_base_url: str
    embedding_api_key: str
    embedding_api_key_source: ConfigValueSource
    embedding_model: str

    @property
    def chat_api_key_configured(self) -> bool:
        return bool(self.chat_api_key)

    @property
    def embedding_api_key_configured(self) -> bool:
        return bool(self.embedding_api_key)


@dataclass(frozen=True, slots=True)
class EffectiveOperationsConfig:
    hitl_notify_auto_approve: bool
    monitor_probe_timeout_seconds: float
    monitor_sweep_interval_seconds: float
    cmdb_diff_interval_seconds: float
```

`get_effective_llm_config()` reads all eight rows in one query, merges missing rows from `settings`, decrypts only non-null API-key database values, and treats a null API-key row as an explicit empty value. `get_effective_operations_config()` reads four rows in one query and validates the merged result through `OperationsSystemConfigUpdate` so malformed manual DB edits fail closed.

`save_llm_config()` always upserts the six non-secret values. It touches an API-key row only when a new key is provided or a clear flag is true. `save_operations_config()` upserts exactly four canonical string values. Both functions flush only. `build_system_config_response()` resolves both snapshots and constructs the three response models above without ever copying `chat_api_key` or `embedding_api_key` into a response field.

- [ ] **Step 7: Run focused tests and static checks**

```powershell
uv run pytest tests/test_data_encryption.py tests/test_system_config_service.py tests/test_cmdb_credential.py -v
uv run ruff check app/core/config.py app/core/data_encryption.py app/core/cmdb_credential.py app/schemas/system_config.py app/services/system_config.py tests/test_data_encryption.py tests/test_system_config_service.py tests/test_cmdb_credential.py
uv run mypy app/core/data_encryption.py app/core/cmdb_credential.py app/schemas/system_config.py app/services/system_config.py
```

Expected: all commands pass; tests assert neither `repr()` nor response-domain objects expose `SecretStr` plaintext.
The CMDB compatibility test must prove existing Fernet ciphertext decrypts without a database rewrite.

- [ ] **Step 8: Commit Task 2**

```powershell
git add backend/app/core/config.py backend/app/core/data_encryption.py backend/app/core/cmdb_credential.py backend/app/schemas/system_config.py backend/app/services/system_config.py backend/.env.example backend/tests/conftest.py backend/tests/test_data_encryption.py backend/tests/test_system_config_service.py backend/tests/test_cmdb_credential.py
git commit -m "新增类型安全的系统配置服务" -m "- 建立 12 个允许配置键的数据库优先与环境变量回退规则
- 复用 CMDB_CREDENTIAL_KEY 加密设备密码和 LLM API Key，并保留原 CMDB 加解密接口
- 对 URL、费用和 HITL/监控数值范围做前后端可复用的明确校验"
```

---

### Task 3: Seed the management permission and four operational defaults

**Files:**
- Modify: `backend/init_db.py`
- Create: `backend/tests/test_system_config_seeds.py`
- Modify: `backend/tests/test_hitl_permission_seeds.py`

**Interfaces:**
- Consumes: `OPERATIONS_CONFIG_KEYS` and `system_config_crud.create_missing()` from Tasks 1–2.
- Produces: `system_config:manage` permission seed and `seed_system_configs() -> int` used by `bootstrap()`.

- [ ] **Step 1: Write failing seed-contract tests**

```python
def test_system_config_permission_is_seeded_once() -> None:
    codes = [item["code"] for item in SEED_PERMISSIONS]
    assert "system_config:manage" in codes
    assert len(codes) == len(set(codes))


async def test_seed_system_configs_creates_only_four_operational_keys(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(init_db, "AsyncSessionLocal", session_factory)

    assert await init_db.seed_system_configs() == 4
    assert await init_db.seed_system_configs() == 0

    async with session_factory() as db:
        rows = await system_config_crud.get_by_keys(db, ALL_SYSTEM_CONFIG_KEYS)
    assert set(rows) == set(OPERATIONS_CONFIG_KEYS)
    assert not set(rows).intersection(LLM_CONFIG_KEYS)
```

Add a test that pre-inserts `MONITOR_SWEEP_INTERVAL_SECONDS=45.0`, runs the seed, and asserts the custom value remains `45.0`.

- [ ] **Step 2: Run the seed tests and verify they fail**

```powershell
uv run pytest tests/test_system_config_seeds.py tests/test_hitl_permission_seeds.py -v
```

Expected: failure because the permission and `seed_system_configs()` are absent.

- [ ] **Step 3: Add the permission and exact seed function**

Append this permission definition to `SEED_PERMISSIONS`:

```python
{
    "name": "管理系统配置",
    "code": "system_config:manage",
    "module": "系统配置",
    "description": "查看并修改 LLM、HITL 与监控运行配置",
}
```

Build the four values from the current validated Settings instance so an existing deployment’s `.env` values are preserved on first bootstrap:

```python
def _system_config_seed_values() -> dict[str, str]:
    return {
        "HITL_NOTIFY_AUTO_APPROVE": (
            "true" if settings.HITL_NOTIFY_AUTO_APPROVE else "false"
        ),
        "MONITOR_PROBE_TIMEOUT_SECONDS": str(
            settings.MONITOR_PROBE_TIMEOUT_SECONDS
        ),
        "MONITOR_SWEEP_INTERVAL_SECONDS": str(
            settings.MONITOR_SWEEP_INTERVAL_SECONDS
        ),
        "CMDB_DIFF_INTERVAL_SECONDS": str(settings.CMDB_DIFF_INTERVAL_SECONDS),
    }


async def seed_system_configs() -> int:
    async with AsyncSessionLocal() as db:
        created = await system_config_crud.create_missing(
            db,
            _system_config_seed_values(),
            updated_by_user_id=None,
        )
        if created:
            await log_audit(
                db,
                user_id=None,
                action="bootstrap_system_configs",
                target="system_configs",
                detail=f"种子写入 {created} 条运行配置",
                ip="local",
            )
        await db.commit()
        return created
```

Call `seed_system_configs()` from `bootstrap()` after `seed_permissions()` and before superuser handling. Do not reference any of the eight `LLM_*` keys from `_system_config_seed_values()`.

- [ ] **Step 4: Run tests**

```powershell
uv run pytest tests/test_system_config_seeds.py tests/test_hitl_permission_seeds.py -v
uv run ruff check init_db.py tests/test_system_config_seeds.py tests/test_hitl_permission_seeds.py
uv run mypy init_db.py
```

Expected: all commands pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add backend/init_db.py backend/tests/test_system_config_seeds.py backend/tests/test_hitl_permission_seeds.py
git commit -m "增加系统配置权限与运行参数种子" -m "- 新增 system_config:manage 权限供非超管角色显式授权
- 幂等落库 HITL 和监控四项基础值，并保留已存在的管理员配置
- 明确排除全部 LLM 与 Embedding 参数，避免把模型地址和密钥写进种子定义"
```

---

### Task 4: Expose audited RBAC-protected system-config APIs

**Files:**
- Create: `backend/app/api/v1/system_config.py`
- Modify: `backend/app/api/router.py`
- Create: `backend/tests/test_system_config_api.py`

**Interfaces:**
- Consumes: request/response schemas and save/effective service functions from Task 2; `require_permission("system_config:manage")`; `log_audit()`.
- Produces: `GET /system-config`、`PUT /system-config/llm`、`PUT /system-config/operations`.

- [ ] **Step 1: Write failing API authorization and redaction tests**

```python
async def test_regular_user_without_permission_cannot_read_or_update(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    assert (await client.get(
        "/api/v1/system-config", headers=auth_headers
    )).status_code == 403
    assert (await client.put(
        "/api/v1/system-config/operations",
        headers=auth_headers,
        json={
            "hitl_notify_auto_approve": False,
            "monitor_probe_timeout_seconds": 3,
            "monitor_sweep_interval_seconds": 30,
            "cmdb_diff_interval_seconds": 3600,
        },
    )).status_code == 403


async def test_superuser_can_save_api_key_but_response_and_audit_are_redacted(
    client: AsyncClient,
    superuser_headers: dict[str, str],
    db_session: AsyncSession,
) -> None:
    secret = "sk-do-not-return-this"
    response = await client.put(
        "/api/v1/system-config/llm",
        headers=superuser_headers,
        json={
            "chat_base_url": "https://llm.example/v1",
            "chat_api_key": secret,
            "clear_chat_api_key": False,
            "chat_model": "chat-model",
            "chat_input_cost_per_million_usd": 1.5,
            "chat_output_cost_per_million_usd": 2.5,
            "embedding_base_url": "https://embedding.example/v1",
            "clear_embedding_api_key": False,
            "embedding_model": "embedding-model",
        },
    )
    assert response.status_code == 200
    assert secret not in response.text
    assert response.json()["data"]["llm"]["chat_api_key_configured"] is True

    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "update_llm_system_config")
    )).scalars().all()
    assert logs
    assert secret not in logs[-1].detail
```

Also cover: unauthenticated 401; a non-superuser explicitly assigned `system_config:manage` succeeds; malformed ranges return 422; clear writes an explicit null row; omitted API key preserves the prior ciphertext; response header contains `Cache-Control: no-store`; operational update writes `update_operations_system_config` in the same transaction.

- [ ] **Step 2: Run API tests and verify 404/failure**

```powershell
uv run pytest tests/test_system_config_api.py -v
```

Expected: requests return 404 before route registration.

- [ ] **Step 3: Implement response mapping and three routes**

```python
router = APIRouter()


@router.get("", response_model=ResponseEnvelope[SystemConfigResponse])
async def get_system_config(
    response: Response,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("system_config:manage")),
) -> ResponseEnvelope[SystemConfigResponse]:
    response.headers["Cache-Control"] = "no-store"
    data = await build_system_config_response(db)
    return success_response(data)
```

Both PUT endpoints accept `Request` for the client IP, call exactly one save service, log only changed key names and whether a secret was replaced/cleared, commit once, then return a freshly resolved redacted `SystemConfigResponse` with `Cache-Control: no-store`.

Convert `DataEncryptionKeyMissingError` into HTTP 422 with a safe message instructing the administrator to configure the existing `CMDB_CREDENTIAL_KEY`; convert `DataDecryptError` into HTTP 500 without returning ciphertext. The message must clarify that this shared key also protects CMDB credentials, so an administrator must restore the original key rather than casually generating a replacement.

- [ ] **Step 4: Register the router and run focused checks**

```python
api_router.include_router(
    system_config_router,
    prefix="/system-config",
    tags=["系统配置"],
)
```

```powershell
uv run pytest tests/test_system_config_api.py -v
uv run ruff check app/api/v1/system_config.py app/api/router.py tests/test_system_config_api.py
uv run mypy app/api/v1/system_config.py
```

Expected: all commands pass.

- [ ] **Step 5: Commit Task 4**

```powershell
git add backend/app/api/v1/system_config.py backend/app/api/router.py backend/tests/test_system_config_api.py
git commit -m "新增受权限保护的系统配置接口" -m "- 提供模型配置、运行配置的读取与分组更新 API
- 超级管理员或 system_config:manage 用户方可访问，所有写入与审计同事务提交
- API Key 仅返回配置状态并设置 no-store，避免响应或审计泄漏秘密值"
```

---

### Task 5: Make LLM and Embedding calls consume database overrides

**Files:**
- Modify: `backend/app/core/llm.py:21-57,260-379`
- Modify: `backend/app/agent/loop.py:56-84`
- Modify: `backend/app/agent/chat_turn.py:94-133`
- Modify: `backend/app/agent/knowledge_tools.py:96-109`
- Modify: `backend/app/services/knowledge_ingestion.py:70-112`
- Modify test: `backend/tests/test_agent_llm.py`
- Modify test: `backend/tests/test_agent_loop.py`
- Modify test: `backend/tests/test_chat_turn.py`
- Modify test: `backend/tests/test_knowledge_tools_semantic_search.py`
- Modify test: `backend/tests/test_knowledge_ingestion.py`

**Interfaces:**
- Consumes: `get_effective_llm_config(db)` from Task 2.
- Produces: optional `db: AsyncSession | None` keyword on `chat()` and `embed()`; existing injected fake-client/fake-chat paths remain compatible.

- [ ] **Step 1: Write failing database-override request tests**

Use `httpx.MockTransport` and an encrypted database key; assert request host, Authorization header, model body and cost calculation without network access.

```python
async def test_chat_uses_database_model_config(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypted = encrypt_secret("db-chat-key")
    await system_config_crud.upsert_values(
        db_session,
        {
            "LLM_CHAT_BASE_URL": "https://db-chat.example/v1",
            "LLM_CHAT_API_KEY": encrypted,
            "LLM_CHAT_MODEL": "db-chat-model",
            "LLM_CHAT_INPUT_COST_PER_MILLION_USD": "2.0",
            "LLM_CHAT_OUTPUT_COST_PER_MILLION_USD": "4.0",
        },
        updated_by_user_id=None,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(
            "https://db-chat.example/v1/chat/completions"
        )
        assert request.headers["Authorization"] == "Bearer db-chat-key"
        assert json.loads(request.content)["model"] == "db-chat-model"
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {"content": "ok", "tool_calls": []},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    def fake_build_client(config: ModelConfig) -> httpx.AsyncClient:
        headers = (
            {"Authorization": f"Bearer {config.api_key}"}
            if config.api_key
            else {}
        )
        return httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            timeout=config.timeout_seconds,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("app.core.llm._build_client", fake_build_client)

    result = await chat(
        "local-chat",
        [ChatMessage(role="user", content="hi")],
        db=db_session,
    )
    assert result.cost_usd == pytest.approx(0.00004)
```

Keep the public `chat()`/`embed()` signatures free of a transport-only testing option. The test replaces the private `_build_client()` exactly as above, so production request construction—including base URL and Authorization headers—is still exercised without a real network call.

Add the equivalent Embedding test for `LLM_EMBEDDING_BASE_URL/API_KEY/MODEL`. Add regression tests proving calls without `db` still use `MODELS` environment fallback and injected chat mocks are not forced to accept a new `db` keyword.

- [ ] **Step 2: Run focused tests and verify database values are ignored**

```powershell
uv run pytest tests/test_agent_llm.py tests/test_agent_loop.py tests/test_chat_turn.py tests/test_knowledge_tools_semantic_search.py tests/test_knowledge_ingestion.py -v
```

Expected: the new tests observe the environment model config instead of the database override.

- [ ] **Step 3: Resolve a typed ModelConfig per call**

Keep `MODELS` as the code-owned registry and backward-compatible fallback. Add a private resolver:

```python
async def _resolve_model_config(
    model_key: str,
    db: AsyncSession | None,
) -> ModelConfig:
    base = MODELS.get(model_key)
    if base is None:
        raise LlmRequestError(
            f"unknown model key {model_key!r}; register it in MODELS first"
        )
    if db is None:
        return base

    effective = await get_effective_llm_config(db)
    if model_key == "local-chat":
        return replace(
            base,
            base_url=effective.chat_base_url,
            api_key=effective.chat_api_key,
            request_model=effective.chat_model,
            input_cost_per_million_usd=(
                effective.chat_input_cost_per_million_usd
            ),
            output_cost_per_million_usd=(
                effective.chat_output_cost_per_million_usd
            ),
        )
    if model_key == "local-embedding":
        return replace(
            base,
            base_url=effective.embedding_base_url,
            api_key=effective.embedding_api_key,
            request_model=effective.embedding_model,
        )
    return base
```

`chat()` and `embed()` call this resolver before capability validation and request creation. Translate secret-key/decrypt errors into `LlmRequestError` with safe Chinese text.

- [ ] **Step 4: Thread the existing DB session through every production callsite**

- `run_loop()` passes `db=db` only when its `chat_fn` is the real `chat`; injected test/model functions keep their current kwargs.
- `run_chat_turn()` passes `db=db` when `chat_fn is None`; an injected `chat_fn` remains untouched.
- `kb_semantic_search()` calls `embed(..., db=db)`.
- `ingest_document()` calls `embed(..., db=db)`.
- Child Agents already run the default `chat` through `run_loop()` with their child transaction, so they receive the same database override without a separate cache.

```python
if chat_fn is chat:
    result = await chat_fn(model_key, history, tools=tools, db=db)
else:
    result = await chat_fn(model_key, history, tools=tools)
```

- [ ] **Step 5: Run the focused suite and type checks**

```powershell
uv run pytest tests/test_agent_llm.py tests/test_agent_loop.py tests/test_chat_turn.py tests/test_knowledge_tools_semantic_search.py tests/test_knowledge_ingestion.py -v
uv run ruff check app/core/llm.py app/agent/loop.py app/agent/chat_turn.py app/agent/knowledge_tools.py app/services/knowledge_ingestion.py
uv run mypy app/core/llm.py app/agent/loop.py app/agent/chat_turn.py app/agent/knowledge_tools.py app/services/knowledge_ingestion.py
```

Expected: all commands pass and no test opens a real network connection.

- [ ] **Step 6: Commit Task 5**

```powershell
git add backend/app/core/llm.py backend/app/agent/loop.py backend/app/agent/chat_turn.py backend/app/agent/knowledge_tools.py backend/app/services/knowledge_ingestion.py backend/tests/test_agent_llm.py backend/tests/test_agent_loop.py backend/tests/test_chat_turn.py backend/tests/test_knowledge_tools_semantic_search.py backend/tests/test_knowledge_ingestion.py
git commit -m "让模型调用读取数据库系统配置" -m "- Chat、Embedding、根 Agent、子 Agent 与知识库链路统一使用数据库覆盖值
- 保留 MODELS 登记表和环境变量回退，避免升级后未配置页面时中断服务
- 测试使用 MockTransport 验证地址、模型、鉴权和费用计算，不产生真实请求"
```

---

### Task 6: Make HITL and background jobs read operational settings dynamically

**Files:**
- Modify: `backend/app/agent/hitl.py:286-307`
- Modify: `backend/app/services/monitor_sweep.py:44-84`
- Modify: `backend/app/services/cmdb_diff.py:79-96`
- Modify test: `backend/tests/test_agent_hitl.py`
- Modify test: `backend/tests/test_monitor_sweep.py`
- Modify test: `backend/tests/test_cmdb_diff.py`

**Interfaces:**
- Consumes: `get_effective_operations_config(db)` from Task 2.
- Produces: database-driven next-proposal/next-cycle behavior while preserving explicit interval/timeout injection in tests.

- [ ] **Step 1: Write failing runtime-consumer tests**

```python
async def test_database_setting_can_auto_approve_notify_when_env_is_false(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    await system_config_crud.upsert_values(
        db_session,
        {"HITL_NOTIFY_AUTO_APPROVE": "true"},
        updated_by_user_id=None,
    )
    summary = await propose_action(
        db_session,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="notify",
        asset_id=asset.id,
        payload={"message": "请检查设备"},
        reason="告警通知",
        actor_user_id=user.id,
    )
    assert summary.status == "EXECUTED"
```

Add a monitor test that stores timeout `7.5`, monkeypatches `probe_tcp`, runs one sweep, and asserts `timeout_seconds == 7.5`. Add loop tests that return database intervals `12` and `120`, monkeypatch `asyncio.sleep` to record the first delay then raise `CancelledError`, and assert the recorded values. Explicit `interval_seconds=` arguments must continue to override the DB value for deterministic tests/manual invocations.

- [ ] **Step 2: Run the focused tests and verify they still use Settings**

```powershell
uv run pytest tests/test_agent_hitl.py tests/test_monitor_sweep.py tests/test_cmdb_diff.py -v
```

Expected: new tests fail because the database rows are ignored.

- [ ] **Step 3: Replace static HITL reads with an effective snapshot**

```python
operations = await get_effective_operations_config(db)
if (
    action_type == "notify"
    and operations.hitl_notify_auto_approve
) or (
    action_type == "device_query"
    and policy_decision == "whitelist"
    and asset.credential_type != "dynamic"
):
    await decide_proposal(
        db,
        proposal_id=proposal.id,
        approve=True,
        reviewed_by_user_id=actor_user_id,
        publisher=publisher,
    )
```

The setting must affect only `notify`; do not widen auto-approval to `device_query`、`device_control` or other future actions.

- [ ] **Step 4: Read monitor timeout and intervals once per cycle**

`run_monitor_sweep_once()` accepts an optional explicit timeout. If absent, it resolves operations through the caller’s DB session. `run_monitor_sweep_loop()` resolves once at the start of each round, passes that timeout into the sweep, and sleeps the freshly read sweep interval after the round.

```python
async def run_monitor_sweep_once(
    db: AsyncSession,
    *,
    probe_timeout_seconds: float | None = None,
) -> int:
    if probe_timeout_seconds is None:
        operations = await get_effective_operations_config(db)
        probe_timeout_seconds = operations.monitor_probe_timeout_seconds
```

`run_cmdb_diff_loop()` reads `cmdb_diff_interval_seconds` through a short-lived `AsyncSessionLocal` before each sleep. Preserve its existing “sleep before first diff” behavior. If an explicit `interval_seconds` is supplied, use it without reading DB.

- [ ] **Step 5: Run focused and static checks**

```powershell
uv run pytest tests/test_agent_hitl.py tests/test_monitor_sweep.py tests/test_cmdb_diff.py -v
uv run ruff check app/agent/hitl.py app/services/monitor_sweep.py app/services/cmdb_diff.py tests/test_agent_hitl.py tests/test_monitor_sweep.py tests/test_cmdb_diff.py
uv run mypy app/agent/hitl.py app/services/monitor_sweep.py app/services/cmdb_diff.py
```

Expected: all commands pass; existing device-query policy behavior is unchanged.

- [ ] **Step 6: Commit Task 6**

```powershell
git add backend/app/agent/hitl.py backend/app/services/monitor_sweep.py backend/app/services/cmdb_diff.py backend/tests/test_agent_hitl.py backend/tests/test_monitor_sweep.py backend/tests/test_cmdb_diff.py
git commit -m "让 HITL 与巡检任务动态读取系统配置" -m "- notify 自动审批在每次提案时读取数据库有效值，且不扩大到其它动作
- TCP 探测超时、监控周期和 CMDB 差异周期在每轮任务重新解析
- 保留测试和运维调用的显式参数覆盖，避免后台任务配置缓存失效"
```

---

### Task 7: Add frontend types, API client and form rules

**Files:**
- Create: `frontend/src/types/system-config.ts`
- Create: `frontend/src/lib/system-config-api.ts`
- Create: `frontend/src/components/system-config/systemConfigFormSchemas.ts`
- Create: `frontend/src/components/system-config/systemConfigFormSchemas.test.ts`

**Interfaces:**
- Consumes: Task 4 JSON contract.
- Produces: `getSystemConfig()`、`updateLlmSystemConfig()`、`updateOperationsSystemConfig()`、`llmConfigFormSchema`、`operationsConfigFormSchema`、`buildLlmUpdatePayload()`.

- [ ] **Step 1: Write failing pure form/payload tests**

```typescript
describe("系统配置表单规则", () => {
  it("拒绝非 HTTP URL 和负数费用", () => {
    const result = llmConfigFormSchema.safeParse({
      chat_base_url: "ftp://llm.example/v1",
      chat_api_key: "",
      clear_chat_api_key: false,
      chat_model: "chat",
      chat_input_cost_per_million_usd: -1,
      chat_output_cost_per_million_usd: 0,
      embedding_base_url: "https://embedding.example/v1",
      embedding_api_key: "",
      clear_embedding_api_key: false,
      embedding_model: "embedding",
    })
    expect(result.success).toBe(false)
  })

  it("密钥输入留空时不发送该字段", () => {
    const payload = buildLlmUpdatePayload(validLlmForm({
      chat_api_key: "",
      clear_chat_api_key: false,
    }))
    expect(payload).not.toHaveProperty("chat_api_key")
  })

  it("明确清空时只发送 clear 标记", () => {
    const payload = buildLlmUpdatePayload(validLlmForm({
      chat_api_key: "",
      clear_chat_api_key: true,
    }))
    expect(payload.clear_chat_api_key).toBe(true)
    expect(payload).not.toHaveProperty("chat_api_key")
  })
})
```

Add exact numeric boundary tests matching backend: timeout `(0,30]`、sweep `[5,3600]`、diff `[60,86400]` and finite non-negative costs.

- [ ] **Step 2: Run the test and verify missing module failure**

```powershell
pnpm test -- src/components/system-config/systemConfigFormSchemas.test.ts
```

Expected: module resolution fails because the schema file does not exist.

- [ ] **Step 3: Define exact API types and client functions**

```typescript
export type ConfigValueSource = "database" | "environment" | "unset"

export interface LlmSystemConfig {
  chat_base_url: string
  chat_model: string
  chat_input_cost_per_million_usd: number
  chat_output_cost_per_million_usd: number
  chat_api_key_configured: boolean
  chat_api_key_source: ConfigValueSource
  embedding_base_url: string
  embedding_model: string
  embedding_api_key_configured: boolean
  embedding_api_key_source: ConfigValueSource
}

export interface OperationsSystemConfig {
  hitl_notify_auto_approve: boolean
  monitor_probe_timeout_seconds: number
  monitor_sweep_interval_seconds: number
  cmdb_diff_interval_seconds: number
}

export interface SystemConfigData {
  llm: LlmSystemConfig
  operations: OperationsSystemConfig
}

export interface LlmSystemConfigUpdate {
  chat_base_url: string
  chat_api_key?: string
  clear_chat_api_key: boolean
  chat_model: string
  chat_input_cost_per_million_usd: number
  chat_output_cost_per_million_usd: number
  embedding_base_url: string
  embedding_api_key?: string
  clear_embedding_api_key: boolean
  embedding_model: string
}

export type OperationsSystemConfigUpdate = OperationsSystemConfig
```

The three client functions must unwrap `response.data.data`, throw if it is absent, and use the shared Axios client so token refresh behavior stays consistent.

- [ ] **Step 4: Implement schemas and safe payload construction**

Use `z.coerce.number()` for number inputs, `.finite().nonnegative()` for costs, and a shared URL refinement that accepts only http/https without embedded credentials, query or fragment. `buildLlmUpdatePayload()` trims secret input and omits blank secrets unless the matching clear flag is true.

```typescript
export function buildLlmUpdatePayload(
  form: LlmConfigFormValues
): LlmSystemConfigUpdate {
  const payload: LlmSystemConfigUpdate = {
    chat_base_url: form.chat_base_url,
    chat_model: form.chat_model,
    chat_input_cost_per_million_usd:
      form.chat_input_cost_per_million_usd,
    chat_output_cost_per_million_usd:
      form.chat_output_cost_per_million_usd,
    embedding_base_url: form.embedding_base_url,
    embedding_model: form.embedding_model,
    clear_chat_api_key: form.clear_chat_api_key,
    clear_embedding_api_key: form.clear_embedding_api_key,
  }
  const chatKey = form.chat_api_key.trim()
  const embeddingKey = form.embedding_api_key.trim()
  if (chatKey && !form.clear_chat_api_key) payload.chat_api_key = chatKey
  if (embeddingKey && !form.clear_embedding_api_key) {
    payload.embedding_api_key = embeddingKey
  }
  return payload
}
```

- [ ] **Step 5: Run frontend unit and static checks**

```powershell
pnpm test -- src/components/system-config/systemConfigFormSchemas.test.ts
pnpm typecheck
pnpm lint
```

Expected: tests and typecheck pass; lint introduces no new warnings.

- [ ] **Step 6: Commit Task 7**

```powershell
git add frontend/src/types/system-config.ts frontend/src/lib/system-config-api.ts frontend/src/components/system-config/systemConfigFormSchemas.ts frontend/src/components/system-config/systemConfigFormSchemas.test.ts
git commit -m "新增系统配置前端数据契约" -m "- 封装系统配置读取与分组更新 API，并与后端响应类型保持一致
- 对模型地址、费用和巡检参数建立与后端相同的 Zod 边界
- 密钥空输入默认保留，只有显式操作才替换或清空，避免误删现有密钥"
```

---

### Task 8: Build the permission-gated System Configuration page

**Files:**
- Create: `frontend/src/components/system-config/LlmConfigCard.tsx`
- Create: `frontend/src/components/system-config/OperationsConfigCard.tsx`
- Create: `frontend/src/components/system-config/OperationsConfigCard.test.tsx`
- Create: `frontend/src/pages/SystemConfigPage.tsx`
- Modify: `frontend/src/lib/constants.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/layout/Sidebar.tsx`
- Modify: `frontend/src/components/layout/Header.tsx`
- Modify: `frontend/src/pages/AuditLogsPage.tsx`

**Interfaces:**
- Consumes: typed API/forms from Task 7 and existing `ProtectedRoute`/`usePermission` superuser behavior.
- Produces: `/system-config` route and a two-card configuration experience available only to `system_config:manage` or superusers. Both cards use `onSaved: (next: SystemConfigData) => void`; `LlmConfigCard` consumes `LlmSystemConfig` and `OperationsConfigCard` consumes `OperationsSystemConfig`.

- [ ] **Step 1: Write failing explanation-copy rendering test**

```tsx
// @vitest-environment jsdom

it("解释四项运行参数的作用和副作用边界", () => {
  render(
    <OperationsConfigCard
      value={{
        hitl_notify_auto_approve: false,
        monitor_probe_timeout_seconds: 3,
        monitor_sweep_interval_seconds: 30,
        cmdb_diff_interval_seconds: 3600,
      }}
      onSaved={vi.fn()}
    />
  )
  expect(screen.getByText(/notify 类型提案会跳过人工审批/)).toBeInTheDocument()
  expect(screen.getByText(/不会自动批准 device_query 或 device_control/)).toBeInTheDocument()
  expect(screen.getByText(/单个 TCP 连接探测允许等待的最长时间/)).toBeInTheDocument()
  expect(screen.getByText(/全部启用目标探测完成后/)).toBeInTheDocument()
  expect(screen.getByText(/只记录差异审计，不自动修改资产/)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run the component test and verify missing component failure**

```powershell
pnpm test -- src/components/system-config/OperationsConfigCard.test.tsx
```

Expected: module resolution fails because the card does not exist.

- [ ] **Step 3: Build the LLM configuration card**

The card contains two visible sections:

- Chat: Base URL、模型名、输入价格/百万 tokens、输出价格/百万 tokens、API Key。
- Embedding: Base URL、模型名、API Key。

For each API Key, display “已配置/未配置” and source badge “数据库/环境变量/未设置”. The password input always initializes empty and uses `autocomplete="new-password"`; never set its value from the GET response. Exact helper copy:

```tsx
<FieldDescription>
  密钥不会从服务端回显。留空会保留当前值；填写新值会替换；勾选“清空密钥”会明确覆盖环境变量回退。
</FieldDescription>
```

Disable the clear checkbox while a new key is non-empty, and disable the secret input while clear is checked. On successful save: show `toast.success("模型配置已保存")`, clear both local secret fields, and call `onSaved(response)`.

- [ ] **Step 4: Build the operations card with exact explanations**

Use a Switch for `hitl_notify_auto_approve` and number inputs for the other three fields. Display these exact descriptions:

```text
开启后，notify 类型提案会跳过人工审批并立即执行；不会自动批准 device_query 或 device_control。
单个 TCP 连接探测允许等待的最长时间，范围为 (0, 30] 秒；下一轮监控探测生效。
全部启用目标探测完成后，到下一轮开始前的全局等待时间，范围为 [5, 3600] 秒。
比较监控在线 IP 与 CMDB 资产台账的周期，范围为 [60, 86400] 秒；只记录差异审计，不自动修改资产。
```

Add a warning Alert when auto-approve is on. Save via `updateOperationsSystemConfig()` and show `toast.success("运行配置已保存")`.

- [ ] **Step 5: Build the page-level loading and retry states**

`SystemConfigPage` fetches once on mount, renders Skeleton cards during loading, a destructive Alert plus “重新加载” button on failure, and the two cards on success. It does not perform permission checks itself because route and sidebar use the centralized permission mechanisms.

```tsx
<PageHeader
  title="系统配置"
  description="管理模型服务、HITL 与监控巡检的运行参数"
/>
```

- [ ] **Step 6: Wire route, permission, navigation, breadcrumb and audit labels**

Add exact constants:

```typescript
SYSTEM_CONFIG: "/system-config"
SYSTEM_CONFIG_MANAGE: "system_config:manage"
```

Protect the route with `PERMISSIONS.SYSTEM_CONFIG_MANAGE`, add “系统配置” under the existing “系统管理” group, and add a breadcrumb mapping. Add audit labels:

```typescript
update_llm_system_config: "更新模型配置",
update_operations_system_config: "更新运行配置",
bootstrap_system_configs: "初始化运行配置",
```

- [ ] **Step 7: Run UI tests and build checks**

```powershell
pnpm test
pnpm typecheck
pnpm lint
pnpm build
```

Expected: all tests/typecheck/build pass; lint has no new warnings beyond the pre-existing project baseline.

- [ ] **Step 8: Commit Task 8**

```powershell
git add frontend/src/components/system-config/LlmConfigCard.tsx frontend/src/components/system-config/OperationsConfigCard.tsx frontend/src/components/system-config/OperationsConfigCard.test.tsx frontend/src/pages/SystemConfigPage.tsx frontend/src/lib/constants.ts frontend/src/App.tsx frontend/src/components/layout/Sidebar.tsx frontend/src/components/layout/Header.tsx frontend/src/pages/AuditLogsPage.tsx
git commit -m "新增系统配置管理页面" -m "- 提供 Chat、Embedding、HITL 与监控参数的分区表单和明确说明
- 页面、路由与导航统一受 system_config:manage 权限保护，超级管理员自动放行
- API Key 永不回显，支持保留、替换和显式清空三种安全操作"
```

---

### Task 9: Document rollout, verify the full repository and prepare migration handoff

**Files:**
- Create: `docs/SYSTEM_CONFIG.md`
- Modify: `README_zh.md`
- Verify: all files changed in Tasks 1–8

**Interfaces:**
- Consumes: completed backend/frontend feature.
- Produces: deployable instructions and a clean verification record; no production DB mutation or real model call.

- [ ] **Step 1: Write the focused system-config operations document**

`docs/SYSTEM_CONFIG.md` must contain these sections with concrete content:

1. 12 个受管键清单及 UI 字段映射。
2. 数据库覆盖、显式空 API Key、环境变量回退的优先级。
3. 现有 `CMDB_CREDENTIAL_KEY` 的生成、备份和共享影响面；泄露会同时暴露 CMDB 密码与 LLM API Key，丢失/替换会使两类密文同时无法解密。
4. 安全轮换步骤必须读取旧密钥、生成新密钥，并在同一维护事务/窗口重新加密 `cmdb_assets.credential_password_encrypted` 与 `system_configs` 中两个 API Key；本功能不提供在线轮换按钮，禁止直接改 `.env` 后重启。
5. `init_db.py` 只创建 4 个运行参数和权限，不创建 8 个 LLM/Embedding 行。
6. 三个 API 路径和 `system_config:manage`/超级管理员权限规则。
7. 四类运行时生效时机。
8. 升级步骤：迁移表结构、运行种子、给角色授权、进入 UI 保存模型配置、确认审计日志。
9. 回滚提示：降级会删除配置表，必须先导出非秘密值并确认；API Key 不应以明文导出。

Add a short feature entry and link in `README_zh.md`; do not duplicate the full document.

- [ ] **Step 2: Run the complete backend verification suite**

From `backend/`:

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy app
```

Expected: 600 existing tests plus the new system-config tests pass; only the two existing PostgreSQL-dependent modules may remain skipped when `TEST_POSTGRES_DATABASE_URL` is absent.

- [ ] **Step 3: Run the complete frontend verification suite**

From `frontend/`:

```powershell
pnpm test
pnpm typecheck
pnpm lint
pnpm build
```

Expected: tests/typecheck/build pass; no new lint warnings.

- [ ] **Step 4: Perform read-only migration and secret scans**

From repository root:

```powershell
rg -n "sk-|LLM_CHAT_API_KEY=.+|LLM_EMBEDDING_API_KEY=.+|CMDB_CREDENTIAL_KEY=.+" backend frontend docs --glob '!backend/.env' --glob '!**/.venv/**' --glob '!**/node_modules/**'
rg -n "revision|down_revision|system_configs" backend/alembic/versions/2026_08_13_1400-a2f6c8d91e37_system_configs.py
git diff --check
git status --short
```

Expected: no real secret values are found; only variable names, tests’ obvious fake values and documentation examples appear. Migration revision points to `f19a7c3e6d84`; diff check is clean.

- [ ] **Step 5: Ask before mutating the user’s PostgreSQL database**

Before running Alembic against the configured development/production database, report the exact target database host/name with the password hidden, ask the user to confirm a backup exists, and wait for approval. After approval, run from `backend/`:

```powershell
uv run alembic current
uv run alembic upgrade head
uv run python init_db.py
uv run alembic current
```

Expected: current revision becomes `a2f6c8d91e37`; initialization reports one new permission and up to four new operational configuration rows on first run, then zero configuration rows on a second run. Do not test real LLM/Embedding endpoints.

- [ ] **Step 6: Commit documentation after verification**

```powershell
git add docs/SYSTEM_CONFIG.md README_zh.md
git commit -m "补充系统配置部署与安全说明" -m "- 记录配置来源优先级、权限边界、种子范围和动态生效时机
- 说明共享 CMDB_CREDENTIAL_KEY 的生成、备份和双重影响面，避免 API Key 以明文进入数据库或文档
- 提供迁移、授权和无真实模型调用的验收流程"
```

---

## Acceptance Checklist

- [ ] 数据库能够保存全部 12 个允许键，无法通过 API 创建任意未知键。
- [ ] 8 个 LLM/Embedding 键不出现在 `init_db.py` 的配置种子集合中。
- [ ] 4 个 HITL/监控键首次初始化会落库，重复初始化不覆盖 UI 修改值。
- [ ] 超级管理员可访问；普通用户无权限返回 403；授予 `system_config:manage` 后可访问和保存。
- [ ] Chat、Embedding、根 Agent、子 Agent、知识库上传与语义检索使用数据库模型配置。
- [ ] notify 自动审批、TCP 超时、monitor sweep 周期、CMDB diff 周期读取数据库有效值。
- [ ] 两个 API Key 在数据库中是 Fernet 密文，GET/PUT 响应和审计没有明文或密文。
- [ ] 项目只使用现有 `CMDB_CREDENTIAL_KEY` 保护 CMDB 密码与 LLM API Key，没有引入第二个系统配置加密环境变量。
- [ ] 既有 CMDB Fernet 密文无需迁移即可通过保留的 `decrypt_credential_password()` 解密。
- [ ] 文档明确共享密钥的双重影响面，并禁止在没有同时重加密两类数据时直接轮换 `.env` 密钥。
- [ ] API Key 留空保持、填写替换、勾选清空三种语义均有后端和前端测试。
- [ ] UI 对 4 个运行参数的作用、范围、副作用与生效时机有中文说明。
- [ ] 页面入口、直接路由和后端 API 三层权限控制一致。
- [ ] 完整后端测试、Ruff、Mypy、前端测试、类型检查、Lint 和 Build 均达到任务中定义的预期。
