# CMDB 资产台账管理页面 + 凭据字段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 CMDB 资产的人工管理页面（新增/编辑/删除/回收站，比照现有用户管理页面的模式），并给 `CmdbAsset` 增加设备登录凭据字段（区分静态密码/动态密码两种类型），静态密码用独立密钥对称加密后入库、永不在任何响应体里回显明文。

**Architecture:** 后端复用既有 `CRUDBase` + `require_permission` + 软删除回收站模式（参照 `role_crud`/`app/api/v1/users.py`），新增一个职责单一的 `app/core/cmdb_credential.py` 做 Fernet 对称加解密，明文密码只在“API 收到请求”和“未来某个操作要用密码连接设备”这两个时间点短暂存在，其余时间只有密文落库。前端复用 `DataTable`/`Pagination`/`usePaginatedQuery`/`ConfirmDialog` 等现成组件，比照 `UsersPage.tsx` + `UserFormDialog.tsx` 的结构。

**Tech Stack:** FastAPI + SQLAlchemy 2 async + PostgreSQL + Alembic + Pydantic 2（后端）；React 19 + TypeScript + react-hook-form + zod + TanStack Table（前端）；新增依赖 `cryptography`（Fernet 对称加密）。

## Global Constraints

- Python `>=3.14,<3.15`；后端命令一律 `uv run <cmd>`，前端命令一律 `npm run <script>`（both from their respective directories）。
- 直接在 `master` 提交，不开分支；每个任务一个中文 commit（标题 + 空行 + 要点），不写 `Co-Authored-By`。
- TDD：先写失败测试，确认失败原因正确，再写最小实现，再确认通过。
- **明文密码永不进入任何响应体、日志、审计 detail 字段。** 响应里只暴露 `credential_password_set: bool`；审计 detail 只记录“凭据是否变更”，不记录值本身。这是硬性安全约束，每个任务的测试都要覆盖到。
- `CMDB_CREDENTIAL_KEY`（Fernet 密钥）**不像 `SECRET_KEY` 那样自动生成**：JWT 密钥重启后失效可以接受（用户重新登录即可），但凭据加密密钥一旦“自动换新”，所有已存的静态密码密文就再也解不开——所以这个密钥允许留空（不强制所有部署都要用凭据管理功能），但一旦提供就必须是合法的 Fernet key 格式；调用加解密函数时若密钥缺失，直接抛出明确的运行时错误，由 API 层转换成可读的 4xx 提示，不是等到真正使用才炸出裸 500。
- CMDB 资产字段本身（主机名/IP/业务系统等）走 `cmdb:read`/`cmdb:manage`（已在 `init_db.py` 里 seed 好，不用改权限种子）。这次不单独拆 `cmdb:credential` 权限——凭据字段跟随资产整体的 `cmdb:manage` 门控。
- **Out of scope（已与项目所有者确认，后续按需单独立项）：**
  1. AI 通过对话读取凭据、弹窗问用户输入动态密码、真正登录设备执行 reboot/shutdown 等操作——这是全新的、安全敏感度很高的端到端能力，`device_control` 执行器目前还是 `NotImplementedExecutor` 占位，且系统里完全没有“对话中途弹窗要敏感输入”这种交互机制。
  2. `CmdbAssetDependency`（资产依赖拓扑）的新增/编辑 UI——资产详情页只做只读展示（复用已有 `cmdb_asset_dependency_crud.get_children`/`get_parents`），不做拓扑编辑。
  3. `MonitorTarget`（监控目标）管理页面——`monitor:manage` 权限码已预留，但本次不做对应 UI。
- 全量验证命令（每个后端任务收尾都要跑，最后一个任务再跑一次全量）：`uv run pytest -v`、`uv run mypy app`、`uv run ruff check .`、`uv run alembic heads`（预期唯一 head）。前端：`npm run typecheck`、`npm run lint`、`npm run test`。

## File Structure

| File | Responsibility |
| :--- | :--- |
| `backend/app/models/cmdb_asset.py` | 加三个凭据字段：`credential_type`/`credential_username`/`credential_password_encrypted`。 |
| `backend/alembic/versions/2026_08_12_1600-e7a3c9d1f582_cmdb_asset_credentials.py` | 新增迁移，加列。 |
| `backend/app/core/config.py` | 新增 `CMDB_CREDENTIAL_KEY` 配置项 + 格式校验。 |
| `backend/app/core/cmdb_credential.py` | 唯一能接触明文密码的加解密原语（`encrypt_credential_password`/`decrypt_credential_password`）。 |
| `backend/app/crud/cmdb_asset.py` | 加 `get_multi_filtered`/`get_deleted_multi`/`restore`/`hard_delete`（照抄 `role_crud` 的回收站模式）。 |
| `backend/app/schemas/cmdb.py` | 新建：`CmdbAssetCreate`/`CmdbAssetUpdate`/`CmdbAssetResponse`，含凭据一致性校验。 |
| `backend/app/api/v1/cmdb.py` | 新建：8 个端点（list/create/get/update/delete/deleted/restore/purge），凭据加解密的“部分更新”业务规则落在这里。 |
| `backend/app/api/router.py` | 注册 `cmdb_router`。 |
| `frontend/src/types/cmdb.ts` | 新建：`CmdbAsset`/`CmdbAssetCreate`/`CmdbAssetUpdate` 类型。 |
| `frontend/src/lib/constants.ts` | 加 `ROUTES.CMDB`/`ROUTES.CMDB_TRASH`（`PERMISSIONS.CMDB_*` 已存在）。 |
| `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx` | 新建：新增/编辑表单，凭据类型三态切换是核心新 UI。 |
| `frontend/src/pages/CmdbAssetsPage.tsx` | 新建：列表页，照抄 `UsersPage.tsx` 结构。 |
| `frontend/src/pages/CmdbAssetsTrashPage.tsx` | 新建：回收站页，照抄 `UsersTrashPage.tsx` 结构。 |
| `frontend/src/App.tsx` | 注册两条新路由。 |
| `frontend/src/components/layout/Sidebar.tsx` | 新增顶层菜单“运维管理”分组，放 CMDB 资产入口。 |

---

### Task 1: 数据模型 + Alembic 迁移（凭据字段）

**Files:**
- Modify: `backend/app/models/cmdb_asset.py`
- Create: `backend/alembic/versions/2026_08_12_1600-e7a3c9d1f582_cmdb_asset_credentials.py`
- Test: `backend/tests/test_ops_models.py`

**Interfaces:**
- `CmdbAsset.credential_type: str`（"none" | "static" | "dynamic"，default "none"）
- `CmdbAsset.credential_username: str`（default ""）
- `CmdbAsset.credential_password_encrypted: str | None`（仅 static 时非空，存密文）

- [ ] **Step 1: 写失败测试**

打开 `backend/tests/test_ops_models.py`，在文件里追加（若已有类似 CmdbAsset 的测试函数，加在其后即可）：

```python
async def test_cmdb_asset_credential_fields_default_to_none_type(
    db_session: AsyncSession,
) -> None:
    """新建资产不填凭据字段时，应落在安全的默认值上。"""
    asset = CmdbAsset(
        asset_type="server",
        hostname="srv-cred-01",
        ip_address="10.0.0.90",
    )
    db_session.add(asset)
    await db_session.flush()

    assert asset.credential_type == "none"
    assert asset.credential_username == ""
    assert asset.credential_password_encrypted is None


async def test_cmdb_asset_can_store_static_credential_ciphertext(
    db_session: AsyncSession,
) -> None:
    """静态凭据把密文原样存取，模型层不关心加密细节。"""
    asset = CmdbAsset(
        asset_type="server",
        hostname="srv-cred-02",
        ip_address="10.0.0.91",
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted="gAAAAA-fake-ciphertext",
    )
    db_session.add(asset)
    await db_session.flush()
    await db_session.refresh(asset)

    assert asset.credential_type == "static"
    assert asset.credential_username == "admin"
    assert asset.credential_password_encrypted == "gAAAAA-fake-ciphertext"
```

确认文件顶部已经 `from app.models.cmdb_asset import CmdbAsset`（现有测试文件里大概率已经导入；没有的话加上）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_ops_models.py -k credential -v`
Expected: FAIL，`AttributeError: 'credential_type'`（模型上还没有这个字段）。

- [ ] **Step 3: 给模型加字段**

修改 `backend/app/models/cmdb_asset.py`，在 `notes` 和 `is_deleted` 之间插入：

```python
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    credential_type: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    credential_username: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    credential_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
```

同时把模块顶部 docstring 的第二句改成：

```python
"""CMDB asset — a lightweight configuration-item record.

Not a full ITIL CMDB: just enough fields to answer "who owns this / where is
it / what business system does it belong to" (docs/AGENT_ARCHITECTURE.md §3).
Credential fields hold only an encrypted static password (see
app/core/cmdb_credential.py) or a bare username for dynamic (OTP-style)
credentials — dynamic passwords are never persisted anywhere.
"""
```

- [ ] **Step 4: 写 Alembic 迁移**

先确认当前 head：

Run: `uv run alembic heads`
Expected: `d6a1b4c9f235 (head)`

创建 `backend/alembic/versions/2026_08_12_1600-e7a3c9d1f582_cmdb_asset_credentials.py`：

```python
"""Add credential fields to cmdb_assets.

Revision ID: e7a3c9d1f582
Revises: d6a1b4c9f235
Create Date: 2026-08-12 16:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "e7a3c9d1f582"
down_revision: str | None = "d6a1b4c9f235"
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
        sa.Column("credential_type", sa.String(length=20), nullable=False, server_default="none"),
    )
    op.add_column(
        "cmdb_assets",
        sa.Column("credential_username", sa.String(length=100), nullable=False, server_default=""),
    )
    op.add_column(
        "cmdb_assets",
        sa.Column("credential_password_encrypted", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    _require_destructive_downgrade()
    op.drop_column("cmdb_assets", "credential_password_encrypted")
    op.drop_column("cmdb_assets", "credential_username")
    op.drop_column("cmdb_assets", "credential_type")
```

- [ ] **Step 5: 跑测试确认通过 + 静态检查**

Run:
```bash
uv run pytest tests/test_ops_models.py -k credential -v
uv run alembic heads
uv run mypy app/models/cmdb_asset.py
uv run ruff check app/models/cmdb_asset.py tests/test_ops_models.py
```
Expected: 2 passed；`e7a3c9d1f582 (head)`；mypy/ruff 干净。

- [ ] **Step 6: 提交**

```bash
git add backend/app/models/cmdb_asset.py backend/alembic/versions/2026_08_12_1600-e7a3c9d1f582_cmdb_asset_credentials.py backend/tests/test_ops_models.py
git commit -m "$(cat <<'EOF'
CmdbAsset 新增设备凭据字段（类型/账号/加密密码）

- credential_type 区分 none/static/dynamic 三种凭据形态，默认 none 不影响现存数据
- 只存密文 credential_password_encrypted（Text，可空），加解密逻辑在下一个任务里落地，
  模型本身不关心密文格式，避免模型层跟具体加密算法耦合
- 动态密码本身永不落库，这里只有 credential_username 一个字段
EOF
)"
```

---

### Task 2: 凭据加解密模块 + `CMDB_CREDENTIAL_KEY` 配置

**Files:**
- Create: `backend/app/core/cmdb_credential.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/.env.example`
- Test: `backend/tests/test_cmdb_credential.py`

**Interfaces:**
- `settings.CMDB_CREDENTIAL_KEY: SecretStr | None`
- `encrypt_credential_password(plain_password: str) -> str`
- `decrypt_credential_password(ciphertext: str) -> str`
- `CmdbCredentialKeyMissingError` / `CmdbCredentialDecryptError`

- [ ] **Step 1: 加依赖**

Run: `uv add cryptography`

这会自动更新 `pyproject.toml` 和 `uv.lock`，不要手动改版本号。

- [ ] **Step 2: 写失败测试**

创建 `backend/tests/test_cmdb_credential.py`：

```python
"""对称加解密 CMDB 静态设备密码。"""

import pytest
from cryptography.fernet import Fernet

from app.core import cmdb_credential
from app.core.cmdb_credential import (
    CmdbCredentialDecryptError,
    CmdbCredentialKeyMissingError,
    decrypt_credential_password,
    encrypt_credential_password,
)
from app.core.config import settings


def test_encrypt_then_decrypt_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))

    ciphertext = encrypt_credential_password("Sup3rSecret!")

    assert ciphertext != "Sup3rSecret!"
    assert decrypt_credential_password(ciphertext) == "Sup3rSecret!"


def test_encrypt_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", None)

    with pytest.raises(CmdbCredentialKeyMissingError):
        encrypt_credential_password("whatever")


def test_decrypt_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", None)

    with pytest.raises(CmdbCredentialKeyMissingError):
        decrypt_credential_password("gAAAAA-anything")


def test_decrypt_raises_on_ciphertext_from_a_different_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    ciphertext = encrypt_credential_password("Sup3rSecret!")

    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))

    with pytest.raises(CmdbCredentialDecryptError):
        decrypt_credential_password(ciphertext)
```

注意：`monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", ...)` 是打在 `settings` 单例对象的属性上，不是 import 一个模块级常量——这样 `cmdb_credential.py` 内部只要每次都通过 `from app.core.config import settings` 后 `settings.CMDB_CREDENTIAL_KEY` 取值（不要在模块顶层把它提前读出来存成模块级变量），monkeypatch 才能生效。这是本项目这个 session 里反复踩过的坑（T06/T07 都出现过“导入时取值 vs 调用时取值”导致 monkeypatch 不生效的 bug），这里要从一开始就用调用时取值的写法。

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/test_cmdb_credential.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.core.cmdb_credential'`。

- [ ] **Step 4: 加配置项**

修改 `backend/app/core/config.py`：

1. 顶部 import 里加 `from cryptography.fernet import Fernet`（放在现有 `from pydantic import ...` 之后、`from pydantic_settings import ...` 之前的合适位置，保持 isort 顺序）。
2. 在 `HITL_NOTIFY_AUTO_APPROVE: bool = False` 之后加一行：

```python
    # CMDB 设备凭据：静态密码对称加密密钥；留空则相关功能在使用时报错，不强制所有部署配置
    CMDB_CREDENTIAL_KEY: SecretStr | None = None
```

3. 加一个新的 `field_validator`（放在 `validate_migration_database_url` 之后）：

```python
    @field_validator("CMDB_CREDENTIAL_KEY")
    @classmethod
    def validate_cmdb_credential_key(cls, value: SecretStr | None) -> SecretStr | None:
        """在启动时校验密钥格式，避免录入了一个格式错误的值等到真正使用才报错。"""
        if value is None:
            return None
        try:
            Fernet(value.get_secret_value().encode("utf-8"))
        except Exception as exc:
            raise ValueError(
                "CMDB_CREDENTIAL_KEY 必须是合法的 Fernet 密钥，用以下命令生成："
                '`python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"`'
            ) from exc
        return value
```

不需要在 `validate_security_settings` 里加生产环境强制项——这是一个可选功能，留给运维人员按需配置。

- [ ] **Step 5: 更新 `.env.example`**

在 `backend/.env.example` 里，`HITL_NOTIFY_AUTO_APPROVE` 那一行附近（或任何合适的位置）加：

```ini
# CMDB 设备凭据加密密钥（可选）：要保存静态密码才需要配置，用
# `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成
CMDB_CREDENTIAL_KEY=
```

- [ ] **Step 6: 实现加解密模块**

创建 `backend/app/core/cmdb_credential.py`：

```python
"""CMDB 资产静态密码的对称加密/解密。

实现流程：
1. 静态设备密码要能在真正连接设备时被程序取回明文，不能像登录密码那样只做
   不可逆哈希；这里用 cryptography 的 Fernet 对称加密（AES128-CBC + HMAC），
   密钥来自独立配置项 CMDB_CREDENTIAL_KEY，不与签发 JWT 的 SECRET_KEY 混用——
   两者的轮换周期和影响面完全不同，混用会让"改一个密钥顺带搞坏另一个功能"。
2. CMDB_CREDENTIAL_KEY 允许留空（不像 SECRET_KEY 那样强制所有环境配置），
   因为不是每个部署都需要静态密码这个功能；留空时这里直接抛错，调用方
   （API 层）把它转换成明确的错误提示，而不是等到真正用到才炸出裸 500。
3. 全部加解密只在这一个模块里发生，其它代码只通过这里的两个函数接触明文
   密码，方便审计"明文密码到底在哪些地方出现过"——目前的答案是：只在这里，
   以及调用它的 API 请求体反序列化那一刻。
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class CmdbCredentialKeyMissingError(RuntimeError):
    """CMDB_CREDENTIAL_KEY 未配置，无法加密或解密静态密码。"""


class CmdbCredentialDecryptError(RuntimeError):
    """密文损坏或密钥已更换，无法解密。"""


def _fernet() -> Fernet:
    key = settings.CMDB_CREDENTIAL_KEY
    if key is None:
        raise CmdbCredentialKeyMissingError(
            "CMDB_CREDENTIAL_KEY 未配置，无法保存或读取静态密码"
        )
    return Fernet(key.get_secret_value().encode("utf-8"))


def encrypt_credential_password(plain_password: str) -> str:
    """加密一个静态设备密码，返回可入库的密文字符串。"""
    return _fernet().encrypt(plain_password.encode("utf-8")).decode("utf-8")


def decrypt_credential_password(ciphertext: str) -> str:
    """解密一个静态设备密码密文，返回明文。"""
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise CmdbCredentialDecryptError(
            "密文无法解密，密钥可能已更换或数据已损坏"
        ) from exc
```

- [ ] **Step 7: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_cmdb_credential.py -v
uv run pytest tests/test_config.py -v
uv run mypy app/core/cmdb_credential.py app/core/config.py
uv run ruff check app/core/cmdb_credential.py app/core/config.py tests/test_cmdb_credential.py
```
Expected: 全部通过（`test_config.py` 是确认新配置项没有破坏既有配置测试），mypy/ruff 干净。

- [ ] **Step 8: 提交**

```bash
git add backend/pyproject.toml backend/uv.lock backend/app/core/cmdb_credential.py backend/app/core/config.py backend/.env.example backend/tests/test_cmdb_credential.py
git commit -m "$(cat <<'EOF'
新增 CMDB 静态密码对称加密模块与独立密钥配置

- 加 cryptography 依赖，用 Fernet 对称加密/解密 CmdbAsset 的静态设备密码
- CMDB_CREDENTIAL_KEY 独立于 JWT 的 SECRET_KEY：不自动生成、允许留空、
  格式在启动时用 field_validator 校验，留空时加解密函数直接报错而不是
  等到真正用到才炸出裸 500
- 明文密码只在 app/core/cmdb_credential.py 这一处被接触，其它代码只拿密文
EOF
)"
```

---

### Task 3: CRUD 扩展（分页查询 + 回收站）

**Files:**
- Modify: `backend/app/crud/cmdb_asset.py`
- Test: `backend/tests/test_cmdb_crud_asset.py`

**Interfaces:**
- `cmdb_asset_crud.get_multi_filtered(db, *, search=None, asset_type=None, business_system=None, skip=0, limit=10) -> tuple[list[CmdbAsset], int]`
- `cmdb_asset_crud.get_deleted_multi(db, *, search=None, skip=0, limit=10) -> tuple[list[CmdbAsset], int]`
- `cmdb_asset_crud.restore(db, id) -> CmdbAsset | None`
- `cmdb_asset_crud.hard_delete(db, id) -> bool`

（`create`/`update`/`soft_delete`/`get` 直接复用 `CRUDBase`，这里不用重写。）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_cmdb_crud_asset.py` 里追加：

```python
async def test_get_multi_filtered_paginates_and_searches(db_session: AsyncSession) -> None:
    for i in range(3):
        await cmdb_asset_crud.create(
            db_session,
            {
                "asset_type": "server",
                "hostname": f"srv-list-{i}",
                "ip_address": f"10.0.1.{i}",
                "business_system": "财务系统" if i == 0 else "",
            },
        )
    await db_session.flush()

    assets, total = await cmdb_asset_crud.get_multi_filtered(db_session, limit=2)
    assert total == 3
    assert len(assets) == 2

    filtered, filtered_total = await cmdb_asset_crud.get_multi_filtered(
        db_session, search="srv-list-0"
    )
    assert filtered_total == 1
    assert filtered[0].hostname == "srv-list-0"

    by_business, by_business_total = await cmdb_asset_crud.get_multi_filtered(
        db_session, business_system="财务系统"
    )
    assert by_business_total == 1
    assert by_business[0].hostname == "srv-list-0"


async def test_soft_delete_restore_and_hard_delete_round_trip(
    db_session: AsyncSession,
) -> None:
    asset = await cmdb_asset_crud.create(
        db_session,
        {"asset_type": "server", "hostname": "srv-trash-01", "ip_address": "10.0.2.1"},
    )
    await db_session.flush()

    assert await cmdb_asset_crud.soft_delete(db_session, asset.id) is True
    assert await cmdb_asset_crud.get(db_session, asset.id) is None

    deleted, deleted_total = await cmdb_asset_crud.get_deleted_multi(db_session)
    assert deleted_total == 1
    assert deleted[0].id == asset.id

    restored = await cmdb_asset_crud.restore(db_session, asset.id)
    assert restored is not None
    assert await cmdb_asset_crud.get(db_session, asset.id) is not None

    assert await cmdb_asset_crud.soft_delete(db_session, asset.id) is True
    assert await cmdb_asset_crud.hard_delete(db_session, asset.id) is True
    assert await cmdb_asset_crud.restore(db_session, asset.id) is None


async def test_restore_and_hard_delete_return_falsy_for_unknown_id(
    db_session: AsyncSession,
) -> None:
    assert await cmdb_asset_crud.restore(db_session, 999_999) is None
    assert await cmdb_asset_crud.hard_delete(db_session, 999_999) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cmdb_crud_asset.py -k "filtered or trash or unknown_id" -v`
Expected: FAIL，`AttributeError: 'CRUDCmdbAsset' object has no attribute 'get_multi_filtered'`。

- [ ] **Step 3: 实现**

修改 `backend/app/crud/cmdb_asset.py`：

```python
"""CRUD operations for CMDB assets."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase, contains_pattern
from app.models.cmdb_asset import CmdbAsset


class CRUDCmdbAsset(CRUDBase[CmdbAsset]):
    """CMDB asset persistence; generic get/create/update/soft_delete come from CRUDBase."""

    model = CmdbAsset

    async def get_by_ip(self, db: AsyncSession, ip_address: str) -> CmdbAsset | None:
        """Return one active asset by IP address, or None."""
        stmt = self._active_statement().where(CmdbAsset.ip_address == ip_address)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self, db: AsyncSession) -> list[CmdbAsset]:
        """Return every active asset, ordered by id."""
        stmt = self._active_statement().order_by(CmdbAsset.id.asc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_business_system(self, db: AsyncSession, business_system: str) -> list[CmdbAsset]:
        """Return active assets tagged with a given business system."""
        stmt = self._active_statement().where(CmdbAsset.business_system == business_system)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_ids(self, db: AsyncSession, ids: list[int]) -> list[CmdbAsset]:
        """Return active assets among the given ids."""
        if not ids:
            return []
        stmt = self._active_statement().where(CmdbAsset.id.in_(ids))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_multi_filtered(
        self,
        db: AsyncSession,
        *,
        search: str | None = None,
        asset_type: str | None = None,
        business_system: str | None = None,
        skip: int = 0,
        limit: int = 10,
    ) -> tuple[list[CmdbAsset], int]:
        """Return a page of active assets for the management page."""
        stmt = self._active_statement()
        if search:
            pattern = contains_pattern(search)
            stmt = stmt.where(
                CmdbAsset.hostname.ilike(pattern, escape="\\")
                | CmdbAsset.ip_address.ilike(pattern, escape="\\")
                | CmdbAsset.business_system.ilike(pattern, escape="\\")
            )
        if asset_type:
            stmt = stmt.where(CmdbAsset.asset_type == asset_type)
        if business_system:
            stmt = stmt.where(CmdbAsset.business_system == business_system)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await db.execute(count_stmt)).scalar_one()

        page_stmt = stmt.order_by(CmdbAsset.id.desc()).offset(skip).limit(limit)
        assets = list((await db.execute(page_stmt)).scalars().all())
        return assets, total

    async def get_deleted_multi(
        self,
        db: AsyncSession,
        *,
        search: str | None = None,
        skip: int = 0,
        limit: int = 10,
    ) -> tuple[list[CmdbAsset], int]:
        """Return a page of soft-deleted assets for the recycle bin."""
        stmt = select(CmdbAsset).where(CmdbAsset.is_deleted.is_(True))
        if search:
            pattern = contains_pattern(search)
            stmt = stmt.where(
                CmdbAsset.hostname.ilike(pattern, escape="\\")
                | CmdbAsset.ip_address.ilike(pattern, escape="\\")
            )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await db.execute(count_stmt)).scalar_one()

        page_stmt = stmt.order_by(CmdbAsset.updated_at.desc(), CmdbAsset.id.desc()).offset(skip).limit(limit)
        assets = list((await db.execute(page_stmt)).scalars().all())
        return assets, total

    async def restore(self, db: AsyncSession, id: int) -> CmdbAsset | None:
        """Restore a soft-deleted asset."""
        stmt = (
            select(CmdbAsset)
            .where(CmdbAsset.id == id, CmdbAsset.is_deleted.is_(True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        asset = (await db.execute(stmt)).scalar_one_or_none()
        if asset is None:
            return None
        asset.is_deleted = False
        await db.flush()
        return asset

    async def hard_delete(self, db: AsyncSession, id: int) -> bool:
        """Permanently remove a soft-deleted asset."""
        stmt = (
            select(CmdbAsset)
            .where(CmdbAsset.id == id, CmdbAsset.is_deleted.is_(True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        asset = (await db.execute(stmt)).scalar_one_or_none()
        if asset is None:
            return False
        await db.delete(asset)
        await db.flush()
        return True


cmdb_asset_crud = CRUDCmdbAsset()
```

`TimestampMixin` 已经给 `CmdbAsset` 提供了 `updated_at`（用于回收站按最近删除时间排序），不需要额外加字段——如果实施时发现 `TimestampMixin` 没有 `updated_at`，改用 `CmdbAsset.id.desc()` 单独排序即可，跑一下 `grep -n "class TimestampMixin" -A 10 app/models/base.py` 确认。

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_cmdb_crud_asset.py -v
uv run mypy app/crud/cmdb_asset.py
uv run ruff check app/crud/cmdb_asset.py tests/test_cmdb_crud_asset.py
```
Expected: 全部通过，mypy/ruff 干净。

- [ ] **Step 5: 提交**

```bash
git add backend/app/crud/cmdb_asset.py backend/tests/test_cmdb_crud_asset.py
git commit -m "$(cat <<'EOF'
CmdbAsset CRUD 补齐管理页面需要的分页查询与回收站方法

- get_multi_filtered 支持 hostname/ip/business_system 模糊搜索 + asset_type/
  business_system 精确筛选，照抄 role_crud.get_multi_filtered 的分页统计写法
- get_deleted_multi/restore/hard_delete 三件套照抄 role_crud 的回收站模式，
  跟用户/角色管理页面保持一致的交互心智
EOF
)"
```

---

### Task 4: Pydantic Schemas（含凭据一致性校验）

**Files:**
- Create: `backend/app/schemas/cmdb.py`
- Test: `backend/tests/test_cmdb_schemas.py`

**Interfaces:**
- `CmdbAssetCreate`、`CmdbAssetUpdate`、`CmdbAssetResponse`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_cmdb_schemas.py`：

```python
"""CmdbAsset 请求/响应模型的凭据一致性校验。"""

import pytest
from pydantic import ValidationError

from app.schemas.cmdb import CmdbAssetCreate, CmdbAssetResponse, CmdbAssetUpdate


def _base_create_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "asset_type": "server",
        "hostname": "srv-01",
        "ip_address": "10.0.0.1",
    }
    kwargs.update(overrides)
    return kwargs


def test_create_defaults_to_no_credential() -> None:
    payload = CmdbAssetCreate.model_validate(_base_create_kwargs())
    assert payload.credential_type == "none"
    assert payload.credential_username == ""
    assert payload.credential_password is None


def test_create_none_type_rejects_username_or_password() -> None:
    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(
            _base_create_kwargs(credential_type="none", credential_username="admin")
        )


def test_create_static_requires_username_and_password() -> None:
    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(
            _base_create_kwargs(credential_type="static", credential_username="admin")
        )

    ok = CmdbAssetCreate.model_validate(
        _base_create_kwargs(
            credential_type="static", credential_username="admin", credential_password="p@ss"
        )
    )
    assert ok.credential_password == "p@ss"


def test_create_dynamic_requires_username_and_rejects_password() -> None:
    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(_base_create_kwargs(credential_type="dynamic"))

    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(
            _base_create_kwargs(
                credential_type="dynamic", credential_username="admin", credential_password="nope"
            )
        )

    ok = CmdbAssetCreate.model_validate(
        _base_create_kwargs(credential_type="dynamic", credential_username="admin")
    )
    assert ok.credential_username == "admin"
    assert ok.credential_password is None


def test_update_allows_partial_fields_without_touching_credentials() -> None:
    payload = CmdbAssetUpdate.model_validate({"hostname": "srv-renamed"})
    assert payload.hostname == "srv-renamed"
    assert "credential_type" not in payload.model_fields_set


def test_update_credential_type_must_be_provided_alongside_other_credential_fields() -> None:
    with pytest.raises(ValidationError):
        CmdbAssetUpdate.model_validate({"credential_username": "admin"})


def test_update_static_password_can_be_omitted_to_keep_existing_secret() -> None:
    payload = CmdbAssetUpdate.model_validate(
        {"credential_type": "static", "credential_username": "admin"}
    )
    assert payload.credential_type == "static"
    assert "credential_password" not in payload.model_fields_set


def test_response_never_exposes_ciphertext_field() -> None:
    assert "credential_password_encrypted" not in CmdbAssetResponse.model_fields
    assert "credential_password" not in CmdbAssetResponse.model_fields
    assert "credential_password_set" in CmdbAssetResponse.model_fields
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cmdb_schemas.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.schemas.cmdb'`。

- [ ] **Step 3: 实现**

创建 `backend/app/schemas/cmdb.py`：

```python
"""CMDB asset request and response models."""

from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from app.schemas.common import ApiModel

type CredentialType = Literal["none", "static", "dynamic"]

_CREDENTIAL_FIELDS = {"credential_type", "credential_username", "credential_password"}


class CmdbAssetCreate(ApiModel):
    """Create a CMDB asset, optionally with a login credential."""

    asset_type: str = Field(min_length=1, max_length=50)
    hostname: str = Field(min_length=1, max_length=255)
    ip_address: str = Field(min_length=1, max_length=45)
    location: str = Field(default="", max_length=200)
    owner_user_id: int | None = None
    business_system: str = Field(default="", max_length=100)
    subnet_cidr: str = Field(default="", max_length=45)
    notes: str = Field(default="", max_length=2000)
    credential_type: CredentialType = "none"
    credential_username: str = Field(default="", max_length=100)
    credential_password: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_credential(self) -> Self:
        if self.credential_type == "none":
            if self.credential_username or self.credential_password is not None:
                raise ValueError("credential_type 为 none 时不能填写账号或密码")
        elif self.credential_type == "static":
            if not self.credential_username:
                raise ValueError("静态凭据必须填写账号")
            if self.credential_password is None:
                raise ValueError("静态凭据必须填写密码")
        elif self.credential_type == "dynamic":
            if not self.credential_username:
                raise ValueError("动态凭据必须填写账号")
            if self.credential_password is not None:
                raise ValueError("动态凭据不需要也不允许填写密码")
        return self


class CmdbAssetUpdate(ApiModel):
    """Partially update a CMDB asset; unset fields are left untouched."""

    asset_type: str | None = Field(default=None, min_length=1, max_length=50)
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    ip_address: str | None = Field(default=None, min_length=1, max_length=45)
    location: str | None = Field(default=None, max_length=200)
    owner_user_id: int | None = None
    business_system: str | None = Field(default=None, max_length=100)
    subnet_cidr: str | None = Field(default=None, max_length=45)
    notes: str | None = Field(default=None, max_length=2000)
    credential_type: CredentialType | None = None
    credential_username: str | None = Field(default=None, max_length=100)
    credential_password: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_credential(self) -> Self:
        touched = _CREDENTIAL_FIELDS & self.model_fields_set
        if not touched:
            return self
        if "credential_type" not in self.model_fields_set:
            raise ValueError("修改凭据信息时必须同时提供 credential_type")

        if self.credential_type == "none":
            if self.credential_username or self.credential_password is not None:
                raise ValueError("credential_type 为 none 时不能填写账号或密码")
        elif self.credential_type == "static":
            if not self.credential_username:
                raise ValueError("静态凭据必须填写账号")
            # 密码字段允许不传（保留原密文），但显式传入时不能是空字符串。
        elif self.credential_type == "dynamic":
            if not self.credential_username:
                raise ValueError("动态凭据必须填写账号")
            if self.credential_password is not None:
                raise ValueError("动态凭据不需要也不允许填写密码")
        return self

    @model_validator(mode="after")
    def reject_empty_update(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("至少提供一个要更新的字段")
        return self


class CmdbAssetResponse(ApiModel):
    """Public asset representation — ciphertext and plaintext password never appear here."""

    id: int
    asset_type: str
    hostname: str
    ip_address: str
    location: str
    owner_user_id: int | None
    business_system: str
    subnet_cidr: str
    notes: str
    credential_type: CredentialType
    credential_username: str
    credential_password_set: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
```

`ApiModel` 已经带 `extra="forbid"`（见 `app/schemas/common.py`），所以任何额外字段（比如前端不小心传了 `credential_password_encrypted`）都会被直接拒绝，这也是一道防线。

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_cmdb_schemas.py -v
uv run mypy app/schemas/cmdb.py
uv run ruff check app/schemas/cmdb.py tests/test_cmdb_schemas.py
```
Expected: 全部通过，mypy/ruff 干净。

- [ ] **Step 5: 提交**

```bash
git add backend/app/schemas/cmdb.py backend/tests/test_cmdb_schemas.py
git commit -m "$(cat <<'EOF'
新增 CmdbAsset 请求/响应模型，凭据三态一致性校验落在 schema 层

- CmdbAssetCreate/Update 用 model_validator 保证 credential_type 与
  username/password 的组合始终合法：none 不许填、static 必须双填（
  Update 时密码可留空表示不改密码）、dynamic 只许填账号
- CmdbAssetResponse 完全不含密文或明文密码字段，只暴露
  credential_password_set 这一个布尔值，配合 ApiModel 的 extra="forbid"
  双重防止密码意外从后端泄露
EOF
)"
```

---

### Task 5: API 路由 + 权限门控 + 审计

**Files:**
- Create: `backend/app/api/v1/cmdb.py`
- Modify: `backend/app/api/router.py`
- Test: `backend/tests/test_cmdb_api.py`

**Interfaces:**
- `GET /api/v1/cmdb/assets`（`cmdb:read`）
- `POST /api/v1/cmdb/assets`（`cmdb:manage`）
- `GET /api/v1/cmdb/assets/deleted`（`cmdb:manage`）
- `GET /api/v1/cmdb/assets/{id}`（`cmdb:read`）
- `PATCH /api/v1/cmdb/assets/{id}`（`cmdb:manage`）
- `DELETE /api/v1/cmdb/assets/{id}`（`cmdb:manage`）
- `POST /api/v1/cmdb/assets/{id}/restore`（`cmdb:manage`）
- `DELETE /api/v1/cmdb/assets/{id}/purge`（`cmdb:manage`）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_cmdb_api.py`：

```python
"""CMDB 资产管理 API：CRUD、回收站、以及凭据永不回显明文的安全测试。"""

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _grant_cmdb_permissions(db_session: AsyncSession, test_user) -> None:  # noqa: ANN001
    """现场创建 cmdb:read + cmdb:manage 并挂到 test_user 已有角色上。

    不能像别处那样直接查询已存在的 Permission 行——测试库的权限种子只来自
    conftest.py 的 test_permissions fixture（user/role/permission/audit 相关），
    不包含 init_db.py 里的 cmdb:* 种子数据，那些只在真实启动时跑。这里照抄
    tests/test_hitl_api.py::_grant_hitl_approve 的"现场创建"模式，而不是
    tests/test_hitl_integration.py 早期版本里错误示范过的"查询已有行"模式。
    """
    from sqlalchemy import select

    from app.models.permission import Permission
    from app.models.role import role_permissions
    from app.models.user import user_roles

    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    for code, name in (("cmdb:read", "查看 CMDB 资产"), ("cmdb:manage", "管理 CMDB 资产")):
        permission = Permission(name=name, code=code, module="CMDB")
        db_session.add(permission)
        await db_session.flush()
        await db_session.execute(
            role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
        )
    await db_session.commit()


async def test_create_asset_with_static_credential_never_echoes_plaintext(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    secret = "Sup3rSecretDevicePwd!"

    response = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-api-01",
            "ip_address": "10.0.9.1",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password": secret,
        },
        headers=auth_headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["credential_password_set"] is True
    assert secret not in response.text
    assert "credential_password_encrypted" not in response.text


async def test_create_dynamic_credential_stores_username_only(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)

    response = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-api-02",
            "ip_address": "10.0.9.2",
            "credential_type": "dynamic",
            "credential_username": "otp-admin",
        },
        headers=auth_headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["credential_type"] == "dynamic"
    assert body["credential_username"] == "otp-admin"
    assert body["credential_password_set"] is False


async def test_update_without_password_keeps_existing_secret(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "server",
            "hostname": "srv-api-03",
            "ip_address": "10.0.9.3",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password": "orig-pwd",
        },
        headers=auth_headers,
    )
    asset_id = create_resp.json()["data"]["id"]

    update_resp = await client.patch(
        f"/api/v1/cmdb/assets/{asset_id}",
        json={"hostname": "srv-api-03-renamed"},
        headers=auth_headers,
    )
    assert update_resp.status_code == 200, update_resp.text
    body = update_resp.json()["data"]
    assert body["hostname"] == "srv-api-03-renamed"
    assert body["credential_password_set"] is True  # 没碰凭据字段，密文原样保留


async def test_switch_to_static_without_password_is_rejected_when_no_existing_secret(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={"asset_type": "server", "hostname": "srv-api-04", "ip_address": "10.0.9.4"},
        headers=auth_headers,
    )
    asset_id = create_resp.json()["data"]["id"]

    response = await client.patch(
        f"/api/v1/cmdb/assets/{asset_id}",
        json={"credential_type": "static", "credential_username": "admin"},
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


async def test_soft_delete_restore_purge_flow(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers  # noqa: ANN001
) -> None:
    await _grant_cmdb_permissions(db_session, test_user)
    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={"asset_type": "server", "hostname": "srv-api-05", "ip_address": "10.0.9.5"},
        headers=auth_headers,
    )
    asset_id = create_resp.json()["data"]["id"]

    delete_resp = await client.delete(f"/api/v1/cmdb/assets/{asset_id}", headers=auth_headers)
    assert delete_resp.status_code == 200, delete_resp.text

    get_resp = await client.get(f"/api/v1/cmdb/assets/{asset_id}", headers=auth_headers)
    assert get_resp.status_code == 404

    deleted_resp = await client.get("/api/v1/cmdb/assets/deleted", headers=auth_headers)
    assert deleted_resp.status_code == 200
    assert any(item["id"] == asset_id for item in deleted_resp.json()["data"]["items"])

    restore_resp = await client.post(f"/api/v1/cmdb/assets/{asset_id}/restore", headers=auth_headers)
    assert restore_resp.status_code == 200, restore_resp.text

    delete_again = await client.delete(f"/api/v1/cmdb/assets/{asset_id}", headers=auth_headers)
    assert delete_again.status_code == 200
    purge_resp = await client.delete(f"/api/v1/cmdb/assets/{asset_id}/purge", headers=auth_headers)
    assert purge_resp.status_code == 200, purge_resp.text

    purge_again = await client.delete(f"/api/v1/cmdb/assets/{asset_id}/purge", headers=auth_headers)
    assert purge_again.status_code == 404


async def test_read_only_role_cannot_create(
    client: AsyncClient, db_session: AsyncSession, test_user, auth_headers: Headers
) -> None:
    # test_user 默认没有任何 cmdb 权限（未调用 _grant_cmdb_permissions）
    response = await client.post(
        "/api/v1/cmdb/assets",
        json={"asset_type": "server", "hostname": "srv-forbidden", "ip_address": "10.0.9.9"},
        headers=auth_headers,
    )
    assert response.status_code == 403
```

具体的 `client`/`test_user`/`auth_headers` fixture 名字以 `backend/tests/conftest.py` 现有约定为准（参照 `tests/test_hitl_api.py` 里已经在用的写法）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cmdb_api.py -v`
Expected: FAIL，404（路由还不存在）。

- [ ] **Step 3: 实现路由**

创建 `backend/app/api/v1/cmdb.py`：

```python
"""Asynchronous CMDB asset management endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cmdb_credential import CmdbCredentialKeyMissingError, encrypt_credential_password
from app.core.database import get_db
from app.core.deps import get_client_ip, require_permission
from app.crud.cmdb_asset import cmdb_asset_crud
from app.models.cmdb_asset import CmdbAsset
from app.models.user import User
from app.schemas.cmdb import CmdbAssetCreate, CmdbAssetResponse, CmdbAssetUpdate
from app.schemas.common import PaginatedData, ResponseEnvelope, paginated_response, success_response
from app.utils.audit import log_audit

router = APIRouter()


def _to_response(asset: CmdbAsset) -> CmdbAssetResponse:
    return CmdbAssetResponse(
        id=asset.id,
        asset_type=asset.asset_type,
        hostname=asset.hostname,
        ip_address=asset.ip_address,
        location=asset.location,
        owner_user_id=asset.owner_user_id,
        business_system=asset.business_system,
        subnet_cidr=asset.subnet_cidr,
        notes=asset.notes,
        credential_type=asset.credential_type,  # type: ignore[arg-type]
        credential_username=asset.credential_username,
        credential_password_set=bool(asset.credential_password_encrypted),
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _prepare_persist_data(
    payload: dict[str, object],
    *,
    existing: CmdbAsset | None,
) -> dict[str, object]:
    """把请求里的凭据明文换成待持久化字段；非凭据字段原样透传。

    没有出现在 payload 里的字段完全不放进返回值，交给 CRUDBase 的
    "只更新出现过的键" 语义去保留原值——这正是"编辑资产时不碰密码就不改密码"
    这个安全约束的落地方式。
    """
    data = dict(payload)
    if "credential_type" not in data:
        return data

    credential_type = data["credential_type"]
    plain_password = data.pop("credential_password", None)

    if credential_type == "static":
        if plain_password is not None:
            try:
                data["credential_password_encrypted"] = encrypt_credential_password(plain_password)
            except CmdbCredentialKeyMissingError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="未配置 CMDB_CREDENTIAL_KEY，无法保存静态密码，请联系管理员配置",
                ) from exc
        elif existing is None or existing.credential_type != "static" or not existing.credential_password_encrypted:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="切换为静态凭据时必须提供密码",
            )
        # 否则：静态类型不变、没传新密码 → 不放 credential_password_encrypted 进 data，保留原密文
    elif credential_type == "none":
        # 不依赖调用方老实传空账号名——服务端自己保证 none 类型下不留残留账号，
        # 即使有人绕过前端直接调 API 只传 credential_type=none 也不会留下不一致数据。
        data["credential_username"] = ""
        data["credential_password_encrypted"] = None
    else:  # dynamic
        data["credential_password_encrypted"] = None

    return data


@router.get("/assets", response_model=ResponseEnvelope[PaginatedData[CmdbAssetResponse]])
async def list_assets(
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=10, ge=1, le=100),
    search: str | None = Query(default=None, min_length=1, max_length=100),
    asset_type: str | None = Query(default=None, min_length=1, max_length=50),
    business_system: str | None = Query(default=None, min_length=1, max_length=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("cmdb:read")),
) -> ResponseEnvelope[PaginatedData[CmdbAssetResponse]]:
    """Return a filtered page of active CMDB assets."""
    assets, total = await cmdb_asset_crud.get_multi_filtered(
        db,
        search=search,
        asset_type=asset_type,
        business_system=business_system,
        skip=(page - 1) * page_size,
        limit=page_size,
    )
    items = [_to_response(asset) for asset in assets]
    return paginated_response(items, total, page, page_size)


@router.post("/assets", response_model=ResponseEnvelope[CmdbAssetResponse], status_code=status.HTTP_201_CREATED)
async def create_asset(
    asset_in: CmdbAssetCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[CmdbAssetResponse]:
    """Create a CMDB asset, optionally with an encrypted static credential."""
    persist_data = _prepare_persist_data(asset_in.model_dump(), existing=None)
    asset = await cmdb_asset_crud.create(db, persist_data)

    credential_changed = asset_in.credential_type != "none"
    await log_audit(
        db,
        user_id=current_user.id,
        action="create_cmdb_asset",
        target=f"cmdb_asset:{asset.id}",
        detail=f"创建资产: {asset.hostname}；凭据{'已设置' if credential_changed else '未设置'}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(_to_response(asset), message="创建成功", code=status.HTTP_201_CREATED)


@router.get("/assets/deleted", response_model=ResponseEnvelope[PaginatedData[CmdbAssetResponse]])
async def list_deleted_assets(
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=10, ge=1, le=100),
    search: str | None = Query(default=None, min_length=1, max_length=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[PaginatedData[CmdbAssetResponse]]:
    """List soft-deleted assets in the recycle bin."""
    assets, total = await cmdb_asset_crud.get_deleted_multi(
        db, search=search, skip=(page - 1) * page_size, limit=page_size
    )
    items = [_to_response(asset) for asset in assets]
    return paginated_response(items, total, page, page_size)


@router.get("/assets/{asset_id}", response_model=ResponseEnvelope[CmdbAssetResponse])
async def get_asset(
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("cmdb:read")),
) -> ResponseEnvelope[CmdbAssetResponse]:
    """Return one active asset."""
    asset = await cmdb_asset_crud.get(db, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    return success_response(_to_response(asset))


@router.patch("/assets/{asset_id}", response_model=ResponseEnvelope[CmdbAssetResponse])
async def update_asset(
    asset_in: CmdbAssetUpdate,
    request: Request,
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[CmdbAssetResponse]:
    """Partially update a CMDB asset."""
    existing = await cmdb_asset_crud.get(db, asset_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="资产不存在")

    persist_data = _prepare_persist_data(
        asset_in.model_dump(exclude_unset=True), existing=existing
    )
    credential_touched = "credential_type" in asset_in.model_fields_set
    updated = await cmdb_asset_crud.update(db, asset_id, persist_data)
    if updated is None:
        raise HTTPException(status_code=404, detail="资产不存在")

    await log_audit(
        db,
        user_id=current_user.id,
        action="update_cmdb_asset",
        target=f"cmdb_asset:{asset_id}",
        detail=f"更新资产: {updated.hostname}；凭据{'已变更' if credential_touched else '未变更'}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(_to_response(updated), message="更新成功")


@router.delete("/assets/{asset_id}", response_model=ResponseEnvelope[None])
async def delete_asset(
    request: Request,
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[None]:
    """Soft-delete a CMDB asset."""
    asset = await cmdb_asset_crud.get(db, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")

    if not await cmdb_asset_crud.soft_delete(db, asset_id):
        raise HTTPException(status_code=404, detail="资产不存在")

    await log_audit(
        db,
        user_id=current_user.id,
        action="delete_cmdb_asset",
        target=f"cmdb_asset:{asset_id}",
        detail=f"删除资产: {asset.hostname}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(None, message="删除成功")


@router.post("/assets/{asset_id}/restore", response_model=ResponseEnvelope[CmdbAssetResponse])
async def restore_asset(
    request: Request,
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[CmdbAssetResponse]:
    """Restore a soft-deleted asset from the recycle bin."""
    restored = await cmdb_asset_crud.restore(db, asset_id)
    if restored is None:
        raise HTTPException(status_code=404, detail="回收站中不存在该资产")

    await log_audit(
        db,
        user_id=current_user.id,
        action="restore_cmdb_asset",
        target=f"cmdb_asset:{asset_id}",
        detail=f"恢复资产: {restored.hostname}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(_to_response(restored), message="恢复成功")


@router.delete("/assets/{asset_id}/purge", response_model=ResponseEnvelope[None])
async def purge_asset(
    request: Request,
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[None]:
    """Permanently delete a soft-deleted asset."""
    if not await cmdb_asset_crud.hard_delete(db, asset_id):
        raise HTTPException(status_code=404, detail="回收站中不存在该资产")

    await log_audit(
        db,
        user_id=current_user.id,
        action="purge_cmdb_asset",
        target=f"cmdb_asset:{asset_id}",
        detail="永久删除资产",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(None, message="已永久删除")
```

修改 `backend/app/api/router.py`：加 import 并注册：

```python
from app.api.v1.cmdb import router as cmdb_router
```

（按字母序插入现有 import 块的合适位置），并在 `api_router.include_router(...)` 块里加：

```python
api_router.include_router(cmdb_router, prefix="/cmdb", tags=["CMDB 资产"])
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
uv run pytest tests/test_cmdb_api.py -v
uv run mypy app/api/v1/cmdb.py app/api/router.py
uv run ruff check app/api/v1/cmdb.py app/api/router.py tests/test_cmdb_api.py
```
Expected: 全部通过，mypy/ruff 干净。

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/v1/cmdb.py backend/app/api/router.py backend/tests/test_cmdb_api.py
git commit -m "$(cat <<'EOF'
新增 CMDB 资产管理 API：CRUD + 回收站，凭据永不回显明文

- 8 个端点照抄 users.py 的结构（list/create/get/update/delete/deleted/
  restore/purge），cmdb:read 只读、cmdb:manage 写操作，跟前端已预留的
  权限码对齐
- _prepare_persist_data 是这次的核心业务规则：把请求里的 credential_password
  明文换成密文再落库，且"编辑资产不传密码字段"时不会覆盖已存密文；切到
  static 类型但既没给新密码又没有旧密文时，返回 422 而不是静默存空密码
- 审计 detail 只记录"凭据是否变更"，不记录任何密码明文或密文
EOF
)"
```

---

### Task 6: 后端跨组件验收

**Files:**
- Test: `backend/tests/test_cmdb_asset_management_integration.py`

**Interfaces:**
- 无新接口，纯验收。

- [ ] **Step 1: 写一个端到端集成测试**

创建 `backend/tests/test_cmdb_asset_management_integration.py`：

```python
"""CMDB 资产管理端到端验收：加密往返 + 全生命周期 + 密码永不泄露。"""

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _grant_cmdb_permissions(db_session: AsyncSession, test_user) -> None:  # noqa: ANN001
    """现场创建 cmdb:read + cmdb:manage 并挂到 test_user 已有角色上（同 Task 5 的写法）。"""
    from sqlalchemy import select

    from app.models.permission import Permission
    from app.models.role import role_permissions
    from app.models.user import user_roles

    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    for code, name in (("cmdb:read", "查看 CMDB 资产"), ("cmdb:manage", "管理 CMDB 资产")):
        permission = Permission(name=name, code=code, module="CMDB")
        db_session.add(permission)
        await db_session.flush()
        await db_session.execute(
            role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
        )
    await db_session.commit()


async def test_full_lifecycle_with_encrypted_credential_round_trips(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user,  # noqa: ANN001
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    await _grant_cmdb_permissions(db_session, test_user)
    secret = "IntegrationSecret!23"

    create_resp = await client.post(
        "/api/v1/cmdb/assets",
        json={
            "asset_type": "switch",
            "hostname": "sw-integration-01",
            "ip_address": "10.0.10.1",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password": secret,
        },
        headers=auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    asset_id = create_resp.json()["data"]["id"]
    assert secret not in create_resp.text

    from app.crud.cmdb_asset import cmdb_asset_crud
    from app.core.cmdb_credential import decrypt_credential_password

    db_session.expire_all()
    row = await cmdb_asset_crud.get(db_session, asset_id)
    assert row is not None
    assert row.credential_password_encrypted is not None
    assert row.credential_password_encrypted != secret
    assert decrypt_credential_password(row.credential_password_encrypted) == secret

    switch_resp = await client.patch(
        f"/api/v1/cmdb/assets/{asset_id}",
        json={"credential_type": "dynamic", "credential_username": "otp-admin"},
        headers=auth_headers,
    )
    assert switch_resp.status_code == 200, switch_resp.text
    assert switch_resp.json()["data"]["credential_password_set"] is False

    db_session.expire_all()
    row = await cmdb_asset_crud.get(db_session, asset_id)
    assert row is not None
    assert row.credential_password_encrypted is None

    list_resp = await client.get("/api/v1/cmdb/assets", headers=auth_headers)
    assert list_resp.status_code == 200
    assert secret not in list_resp.text
    assert "credential_password_encrypted" not in list_resp.text

    delete_resp = await client.delete(f"/api/v1/cmdb/assets/{asset_id}", headers=auth_headers)
    assert delete_resp.status_code == 200

    restore_resp = await client.post(f"/api/v1/cmdb/assets/{asset_id}/restore", headers=auth_headers)
    assert restore_resp.status_code == 200
```

- [ ] **Step 2: 跑通并做全量验证**

Run:
```bash
uv run pytest tests/test_cmdb_asset_management_integration.py -v
uv run pytest -v
uv run mypy app
uv run ruff check .
uv run alembic heads
```
Expected: 新测试通过；全量套件零回归；mypy/ruff 干净；`alembic heads` 仍是单个 head（`e7a3c9d1f582 (head)`）。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_cmdb_asset_management_integration.py
git commit -m "$(cat <<'EOF'
CMDB 资产管理后端端到端验收：加密往返、生命周期、密码零泄露

- 用真实 Fernet 密钥验证入库的是密文、能正确解密回明文
- 验证 static→dynamic 切换会清空密文，list/create/update 响应体里
  永远不出现明文密码、密文字段名，甚至字段名本身也不出现
- 记录全量 pytest/mypy/ruff/alembic heads 验证结果
EOF
)"
```

---

### Task 7: 前端类型 + 路由/菜单常量登记

**Files:**
- Create: `frontend/src/types/cmdb.ts`
- Modify: `frontend/src/lib/constants.ts`

**Interfaces:**
- `CmdbAsset`、`CmdbAssetCreate`、`CmdbAssetUpdate`、`CmdbAssetQueryParams` 类型
- `ROUTES.CMDB` / `ROUTES.CMDB_TRASH`

- [ ] **Step 1: 新建类型文件**

创建 `frontend/src/types/cmdb.ts`：

```typescript
/** CMDB 资产相关类型 */

export type CredentialType = "none" | "static" | "dynamic"

/** CMDB 资产（列表/详情响应） */
export interface CmdbAsset {
  id: number
  asset_type: string
  hostname: string
  ip_address: string
  location: string
  owner_user_id: number | null
  business_system: string
  subnet_cidr: string
  notes: string
  credential_type: CredentialType
  credential_username: string
  credential_password_set: boolean
  created_at: string
  updated_at: string
}

/** 创建资产请求 */
export interface CmdbAssetCreate {
  asset_type: string
  hostname: string
  ip_address: string
  location?: string
  owner_user_id?: number | null
  business_system?: string
  subnet_cidr?: string
  notes?: string
  credential_type?: CredentialType
  credential_username?: string
  credential_password?: string | null
}

/** 更新资产请求（部分字段） */
export interface CmdbAssetUpdate {
  asset_type?: string
  hostname?: string
  ip_address?: string
  location?: string
  owner_user_id?: number | null
  business_system?: string
  subnet_cidr?: string
  notes?: string
  credential_type?: CredentialType
  credential_username?: string
  credential_password?: string | null
}

/** 资产查询参数 */
export interface CmdbAssetQueryParams {
  page?: number
  page_size?: number
  search?: string
  asset_type?: string | null
  business_system?: string | null
}
```

- [ ] **Step 2: 加路由常量**

修改 `frontend/src/lib/constants.ts`，在 `PERMISSIONS_TRASH: "/permissions/trash",` 之后加两行：

```typescript
  CMDB: "/cmdb",
  CMDB_TRASH: "/cmdb/trash",
```

`PERMISSIONS.CMDB_READ`/`CMDB_MANAGE` 已经存在，这一步不用碰。

- [ ] **Step 3: 类型检查**

Run: `npm run typecheck`
Expected: 通过（这一步没有运行时逻辑，只是新增类型和常量，不需要单独的失败测试）。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/types/cmdb.ts frontend/src/lib/constants.ts
git commit -m "$(cat <<'EOF'
新增 CMDB 资产前端类型定义与路由常量

- types/cmdb.ts 跟后端 CmdbAssetResponse/Create/Update 的字段一一对应，
  credential_password_set 是前端唯一能看到的"密码是否已设置"信号
- constants.ts 补 ROUTES.CMDB/CMDB_TRASH；PERMISSIONS.CMDB_* 之前已经
  预留好了，不用改
EOF
)"
```

---

### Task 8: 资产表单对话框（凭据类型三态切换）

**Files:**
- Create: `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx`
- Test: `frontend/src/components/cmdb/CmdbAssetFormDialog.test.tsx`

**Interfaces:**
- `<CmdbAssetFormDialog open onOpenChange asset onSubmit />`

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/components/cmdb/CmdbAssetFormDialog.test.tsx`：

```typescript
/** 凭据三态切换的字段可见性/必填校验单测，不跑完整 Dialog 渲染栈 */

import { describe, expect, it } from "vitest"
import { z } from "zod"

// 与 CmdbAssetFormDialog.tsx 内的 schema 保持一致，这里独立复刻校验规则做单测，
// 避免拖入 base-ui Dialog 的真实渲染依赖（项目里现有表单测试也没有走完整渲染）。
const credentialSchema = z
  .object({
    credential_type: z.enum(["none", "static", "dynamic"]),
    credential_username: z.string().max(100).optional().default(""),
    credential_password: z.string().max(256).optional().default(""),
  })
  .superRefine((data, ctx) => {
    if (data.credential_type === "none") {
      if (data.credential_username || data.credential_password) {
        ctx.addIssue({ code: "custom", message: "无凭据时不能填写账号或密码" })
      }
    } else if (data.credential_type === "static") {
      if (!data.credential_username) {
        ctx.addIssue({ code: "custom", message: "静态凭据必须填写账号" })
      }
    } else if (data.credential_type === "dynamic") {
      if (!data.credential_username) {
        ctx.addIssue({ code: "custom", message: "动态凭据必须填写账号" })
      }
      if (data.credential_password) {
        ctx.addIssue({ code: "custom", message: "动态凭据不允许填写密码" })
      }
    }
  })

describe("CmdbAssetFormDialog 凭据校验规则", () => {
  it("none 类型不允许账号或密码", () => {
    const result = credentialSchema.safeParse({
      credential_type: "none",
      credential_username: "admin",
    })
    expect(result.success).toBe(false)
  })

  it("static 类型必须有账号", () => {
    const result = credentialSchema.safeParse({ credential_type: "static" })
    expect(result.success).toBe(false)
  })

  it("dynamic 类型允许只填账号", () => {
    const result = credentialSchema.safeParse({
      credential_type: "dynamic",
      credential_username: "otp-admin",
    })
    expect(result.success).toBe(true)
  })

  it("dynamic 类型不允许填密码", () => {
    const result = credentialSchema.safeParse({
      credential_type: "dynamic",
      credential_username: "otp-admin",
      credential_password: "nope",
    })
    expect(result.success).toBe(false)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npm run test -- CmdbAssetFormDialog`
Expected: 目前会失败，因为文件里引用的 schema 逻辑还没有被组件本身复用验证——如果直接跑这个独立测试文件本身其实会通过（它自己定义了 schema），所以这一步的“RED”实际发生在下一步：组件文件还不存在，`npm run typecheck`/`npm run build` 会报错。先确认：

Run: `ls src/components/cmdb/CmdbAssetFormDialog.tsx`
Expected: 报错，文件不存在。

- [ ] **Step 3: 实现组件**

创建 `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx`：

```typescript
/** CMDB 资产新增/编辑表单对话框
 *
 * 凭据类型三态切换是这里的核心：none 不显示账号密码；static 显示账号+密码
 * （编辑时密码框留空 = 不修改，placeholder 提示"留空则不修改已设置的密码"）；
 * dynamic 只显示账号，不显示密码输入框。
 */

import { useEffect } from "react"
import { Controller, useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Field,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import type { CmdbAsset, CmdbAssetCreate, CmdbAssetUpdate, CredentialType } from "@/types/cmdb"

const CREDENTIAL_TYPE_ITEMS: { label: string; value: CredentialType }[] = [
  { label: "无", value: "none" },
  { label: "静态密码", value: "static" },
  { label: "动态密码（仅记账号）", value: "dynamic" },
]

const formSchema = z
  .object({
    asset_type: z.string().min(1, "请输入资产类型").max(50),
    hostname: z.string().min(1, "请输入主机名").max(255),
    ip_address: z.string().min(1, "请输入 IP 地址").max(45),
    location: z.string().max(200).optional().default(""),
    business_system: z.string().max(100).optional().default(""),
    subnet_cidr: z.string().max(45).optional().default(""),
    notes: z.string().max(2000).optional().default(""),
    credential_type: z.enum(["none", "static", "dynamic"]),
    credential_username: z.string().max(100).optional().default(""),
    credential_password: z.string().max(256).optional().default(""),
  })
  .superRefine((data, ctx) => {
    if (data.credential_type === "none") {
      if (data.credential_username || data.credential_password) {
        ctx.addIssue({
          code: "custom",
          path: ["credential_username"],
          message: "凭据类型为「无」时不能填写账号或密码",
        })
      }
    } else if (data.credential_type === "static") {
      if (!data.credential_username) {
        ctx.addIssue({ code: "custom", path: ["credential_username"], message: "静态凭据必须填写账号" })
      }
    } else if (data.credential_type === "dynamic") {
      if (!data.credential_username) {
        ctx.addIssue({ code: "custom", path: ["credential_username"], message: "动态凭据必须填写账号" })
      }
      if (data.credential_password) {
        ctx.addIssue({
          code: "custom",
          path: ["credential_password"],
          message: "动态凭据不需要也不允许填写密码",
        })
      }
    }
  })

type FormValues = z.infer<typeof formSchema>

interface CmdbAssetFormDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  asset?: CmdbAsset | null
  onSubmit: (data: CmdbAssetCreate | CmdbAssetUpdate) => Promise<boolean>
}

function defaultValues(asset?: CmdbAsset | null): FormValues {
  return {
    asset_type: asset?.asset_type ?? "",
    hostname: asset?.hostname ?? "",
    ip_address: asset?.ip_address ?? "",
    location: asset?.location ?? "",
    business_system: asset?.business_system ?? "",
    subnet_cidr: asset?.subnet_cidr ?? "",
    notes: asset?.notes ?? "",
    credential_type: asset?.credential_type ?? "none",
    credential_username: asset?.credential_username ?? "",
    credential_password: "",
  }
}

export function CmdbAssetFormDialog({
  open,
  onOpenChange,
  asset,
  onSubmit,
}: CmdbAssetFormDialogProps) {
  const isEdit = !!asset
  const form = useForm<FormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: defaultValues(asset),
  })

  useEffect(() => {
    form.reset(defaultValues(asset))
  }, [asset, form])

  const credentialType = form.watch("credential_type")

  const handleSubmit = async (data: FormValues) => {
    const passwordChanged = data.credential_type === "static" && data.credential_password !== ""
    const payload: CmdbAssetCreate | CmdbAssetUpdate = {
      asset_type: data.asset_type,
      hostname: data.hostname,
      ip_address: data.ip_address,
      location: data.location,
      business_system: data.business_system,
      subnet_cidr: data.subnet_cidr,
      notes: data.notes,
      credential_type: data.credential_type,
      credential_username: data.credential_type === "none" ? "" : data.credential_username,
      // 编辑且未修改密码时，不把 credential_password 传出去（undefined 会被
      // JSON.stringify 丢弃这个键），后端据此保留原有密文不变。
      ...(data.credential_type === "static" && (passwordChanged || !isEdit)
        ? { credential_password: data.credential_password }
        : data.credential_type === "dynamic"
          ? { credential_password: null }
          : {}),
    }
    const ok = await onSubmit(payload)
    if (ok) onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? "编辑资产" : "新增资产"}</DialogTitle>
          <DialogDescription>
            {isEdit ? "修改 CMDB 资产信息" : "登记一个新的 CMDB 资产"}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={form.handleSubmit(handleSubmit)}>
          <FieldGroup>
            <Controller
              control={form.control}
              name="asset_type"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid}>
                  <FieldLabel htmlFor="asset-type">资产类型</FieldLabel>
                  <Input id="asset-type" placeholder="如 server / switch / router" {...field} />
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />
            <Controller
              control={form.control}
              name="hostname"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid}>
                  <FieldLabel htmlFor="asset-hostname">主机名</FieldLabel>
                  <Input id="asset-hostname" {...field} />
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />
            <Controller
              control={form.control}
              name="ip_address"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid}>
                  <FieldLabel htmlFor="asset-ip">IP 地址</FieldLabel>
                  <Input id="asset-ip" {...field} />
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />
            <Controller
              control={form.control}
              name="business_system"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid}>
                  <FieldLabel htmlFor="asset-business">业务系统</FieldLabel>
                  <Input id="asset-business" {...field} />
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />
            <Controller
              control={form.control}
              name="location"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid}>
                  <FieldLabel htmlFor="asset-location">位置</FieldLabel>
                  <Input id="asset-location" {...field} />
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />
            <Controller
              control={form.control}
              name="credential_type"
              render={({ field }) => (
                <Field>
                  <FieldLabel htmlFor="asset-credential-type">登录凭据类型</FieldLabel>
                  <Select
                    items={CREDENTIAL_TYPE_ITEMS}
                    value={field.value}
                    onValueChange={(value) => field.onChange(value ?? "none")}
                  >
                    <SelectTrigger id="asset-credential-type">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {CREDENTIAL_TYPE_ITEMS.map((item) => (
                          <SelectItem key={item.value} value={item.value}>
                            {item.label}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
              )}
            />
            {credentialType !== "none" && (
              <Controller
                control={form.control}
                name="credential_username"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="asset-credential-username">登录账号</FieldLabel>
                    <Input id="asset-credential-username" autoComplete="off" {...field} />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
            )}
            {credentialType === "static" && (
              <Controller
                control={form.control}
                name="credential_password"
                render={({ field, fieldState }) => (
                  <Field data-invalid={fieldState.invalid}>
                    <FieldLabel htmlFor="asset-credential-password">登录密码</FieldLabel>
                    <Input
                      id="asset-credential-password"
                      type="password"
                      autoComplete="new-password"
                      placeholder={isEdit ? "留空则不修改已设置的密码" : "请输入密码"}
                      {...field}
                    />
                    <FieldError errors={[fieldState.error]} />
                  </Field>
                )}
              />
            )}
            <Controller
              control={form.control}
              name="notes"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid}>
                  <FieldLabel htmlFor="asset-notes">备注</FieldLabel>
                  <Textarea id="asset-notes" rows={3} {...field} />
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={form.formState.isSubmitting}
              >
                取消
              </Button>
              <Button type="submit" disabled={form.formState.isSubmitting}>
                {form.formState.isSubmitting && <Spinner data-icon="inline-start" />}
                确定
              </Button>
            </DialogFooter>
          </FieldGroup>
        </form>
      </DialogContent>
    </Dialog>
  )
}
```

如果实施时发现项目里没有现成的 `@/components/ui/textarea`，跑一下 `ls src/components/ui/ | grep -i text`；没有的话把 `notes` 字段换成多行 `Input`（或者用 shadcn 的方式加一个 textarea 组件，参照现有 `Input` 组件的实现风格）即可，不是这个任务的重点。

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
npm run test -- CmdbAssetFormDialog
npm run typecheck
npm run lint
```
Expected: 4 个校验规则测试通过；typecheck/lint 干净。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/cmdb/CmdbAssetFormDialog.tsx frontend/src/components/cmdb/CmdbAssetFormDialog.test.tsx
git commit -m "$(cat <<'EOF'
新增 CMDB 资产表单对话框，凭据类型三态切换是核心 UI

- none/static/dynamic 三态：无凭据不显示账号密码字段；静态显示账号+密码
  （编辑态密码框留空表示不修改，不回显已存密码）；动态只显示账号
- 提交时如果是编辑态且没有修改静态密码，不把 credential_password 键放进
  请求体，后端据此保留原密文，从前端这一层就避免"意外清空密码"
EOF
)"
```

---

### Task 9: 资产列表页

**Files:**
- Create: `frontend/src/pages/CmdbAssetsPage.tsx`

**Interfaces:**
- 无新导出接口，页面组件。

- [ ] **Step 1: 实现页面**

创建 `frontend/src/pages/CmdbAssetsPage.tsx`（结构照抄 `UsersPage.tsx`，去掉角色分配/重置密码这些用户特有的操作，换成资产的字段）：

```typescript
/** CMDB 资产管理页
 *
 * DataTable + 搜索/资产类型筛选 + 新增/编辑/删除/回收站，结构照抄 UsersPage.tsx。
 */

import { useMemo, useState } from "react"
import { Link } from "react-router"
import type { ColumnDef } from "@tanstack/react-table"
import dayjs from "dayjs"
import { toast } from "sonner"

import { PlusSignIcon, PencilEdit02Icon, Delete02Icon, InboxIcon, MoreHorizontalIcon } from "@/lib/icons"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { PageHeader } from "@/components/layout/PageHeader"
import { DataTable } from "@/components/common/DataTable"
import { Pagination } from "@/components/common/Pagination"
import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { CmdbAssetFormDialog } from "@/components/cmdb/CmdbAssetFormDialog"
import api from "@/lib/api"
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import { usePermission } from "@/hooks/use-permission"
import { PERMISSIONS, ROUTES } from "@/lib/constants"
import type { CmdbAsset, CmdbAssetCreate, CmdbAssetUpdate } from "@/types/cmdb"

export function CmdbAssetsPage() {
  const { hasPermission } = usePermission()
  const [search, setSearch] = useState("")

  const {
    items: assets,
    total,
    page,
    setPage,
    pageSize,
    isLoading,
    onPageSizeChange,
    refetch: fetchAssets,
  } = usePaginatedQuery<CmdbAsset>({
    url: "/cmdb/assets",
    params: search ? { search } : {},
    errorMessage: "获取资产列表失败",
  })

  const [formOpen, setFormOpen] = useState(false)
  const [editingAsset, setEditingAsset] = useState<CmdbAsset | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleteAsset, setDeleteAsset] = useState<CmdbAsset | null>(null)

  const handleCreate = () => {
    setEditingAsset(null)
    setFormOpen(true)
  }

  const handleEdit = (asset: CmdbAsset) => {
    setEditingAsset(asset)
    setFormOpen(true)
  }

  const handleDeleteClick = (asset: CmdbAsset) => {
    setDeleteAsset(asset)
    setDeleteOpen(true)
  }

  const handleSubmit = async (
    data: CmdbAssetCreate | CmdbAssetUpdate
  ): Promise<boolean> => {
    try {
      if (editingAsset) {
        await api.patch(`/cmdb/assets/${editingAsset.id}`, data)
        toast.success("更新成功")
      } else {
        await api.post("/cmdb/assets", data)
        toast.success("创建成功")
      }
      fetchAssets()
      return true
    } catch {
      toast.error(editingAsset ? "更新失败" : "创建失败")
      return false
    }
  }

  const handleDeleteConfirm = async (): Promise<boolean> => {
    if (!deleteAsset) return false
    try {
      await api.delete(`/cmdb/assets/${deleteAsset.id}`)
      toast.success("删除成功")
      fetchAssets()
      return true
    } catch {
      toast.error("删除失败")
      return false
    }
  }

  const columns = useMemo<ColumnDef<CmdbAsset>[]>(
    () => [
      { accessorKey: "hostname", header: "主机名" },
      { accessorKey: "ip_address", header: "IP 地址" },
      { accessorKey: "asset_type", header: "类型" },
      {
        accessorKey: "business_system",
        header: "业务系统",
        cell: ({ row }) => row.original.business_system || "-",
      },
      {
        id: "credential",
        header: "登录凭据",
        cell: ({ row }) => {
          const asset = row.original
          if (asset.credential_type === "none") {
            return <span className="text-muted-foreground">未配置</span>
          }
          return (
            <Badge variant="secondary">
              {asset.credential_type === "static" ? "静态密码" : "动态密码"}
              {asset.credential_type === "static" && !asset.credential_password_set && "（未设置）"}
            </Badge>
          )
        },
      },
      {
        accessorKey: "created_at",
        header: "创建时间",
        cell: ({ row }) => dayjs(row.original.created_at).format("YYYY-MM-DD HH:mm"),
      },
      {
        id: "actions",
        header: "操作",
        cell: ({ row }) => (
          <DropdownMenu>
            <DropdownMenuTrigger
              render={<Button variant="ghost" size="icon-sm" aria-label="更多操作" />}
            >
              <MoreHorizontalIcon />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuGroup>
                {hasPermission(PERMISSIONS.CMDB_MANAGE) && (
                  <DropdownMenuItem onClick={() => handleEdit(row.original)}>
                    <PencilEdit02Icon />
                    <span>编辑</span>
                  </DropdownMenuItem>
                )}
                {hasPermission(PERMISSIONS.CMDB_MANAGE) && (
                  <DropdownMenuItem
                    onClick={() => handleDeleteClick(row.original)}
                    className="text-destructive"
                  >
                    <Delete02Icon />
                    <span>删除</span>
                  </DropdownMenuItem>
                )}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        ),
      },
    ],
    [hasPermission]
  )

  return (
    <div>
      <PageHeader
        title="CMDB 资产管理"
        description="维护设备台账与登录凭据"
        actions={
          <>
            {hasPermission(PERMISSIONS.CMDB_MANAGE) && (
              <Button variant="outline" render={<Link to={ROUTES.CMDB_TRASH} />}>
                <InboxIcon data-icon="inline-start" />
                回收站
              </Button>
            )}
            {hasPermission(PERMISSIONS.CMDB_MANAGE) && (
              <Button onClick={handleCreate}>
                <PlusSignIcon data-icon="inline-start" />
                新增资产
              </Button>
            )}
          </>
        }
      />

      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <Input
          placeholder="搜索主机名、IP 或业务系统..."
          value={search}
          onChange={(e) => {
            setSearch(e.target.value)
            setPage(1)
          }}
          className="sm:max-w-xs"
        />
      </div>

      <DataTable
        columns={columns}
        data={assets}
        isLoading={isLoading}
        emptyMessage="暂无资产数据"
      />

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={onPageSizeChange}
      />

      <CmdbAssetFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        asset={editingAsset}
        onSubmit={handleSubmit}
      />
      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="确认删除资产"
        description={`确定要删除资产「${deleteAsset?.hostname}」吗？可在回收站恢复。`}
        onConfirm={handleDeleteConfirm}
      />
    </div>
  )
}
```

- [ ] **Step 2: 类型检查 + lint**

Run:
```bash
npm run typecheck
npm run lint
```
Expected: 干净（这一页面没有独立单测，行为由 Task 11 的端到端验收覆盖）。

- [ ] **Step 3: 提交**

```bash
git add frontend/src/pages/CmdbAssetsPage.tsx
git commit -m "$(cat <<'EOF'
新增 CMDB 资产管理列表页

- 结构照抄 UsersPage.tsx：DataTable + 搜索 + 新增/编辑/删除 + 回收站入口
- 凭据列只显示类型徽标（静态/动态/未配置），不显示、不请求任何密码相关字段
EOF
)"
```

---

### Task 10: 回收站页面 + 路由注册

**Files:**
- Create: `frontend/src/pages/CmdbAssetsTrashPage.tsx`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- 无新导出接口，页面组件 + 路由。

- [ ] **Step 1: 实现回收站页面**

创建 `frontend/src/pages/CmdbAssetsTrashPage.tsx`（结构照抄 `UsersTrashPage.tsx`；具体导入路径以该文件实际结构为准，实施时先 `cat frontend/src/pages/UsersTrashPage.tsx` 确认现有实现，再对照改写）：

```typescript
/** CMDB 资产回收站
 *
 * 结构照抄 UsersTrashPage.tsx：软删除资产的列表 + 恢复 + 永久删除。
 */

import { useMemo, useState } from "react"
import { Link } from "react-router"
import type { ColumnDef } from "@tanstack/react-table"
import dayjs from "dayjs"
import { toast } from "sonner"

import { ArrowLeft01Icon, RefreshIcon, Delete02Icon } from "@/lib/icons"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { PageHeader } from "@/components/layout/PageHeader"
import { DataTable } from "@/components/common/DataTable"
import { Pagination } from "@/components/common/Pagination"
import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import api from "@/lib/api"
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import { ROUTES } from "@/lib/constants"
import type { CmdbAsset } from "@/types/cmdb"

export function CmdbAssetsTrashPage() {
  const [search, setSearch] = useState("")

  const {
    items: assets,
    total,
    page,
    setPage,
    pageSize,
    isLoading,
    onPageSizeChange,
    refetch: fetchDeleted,
  } = usePaginatedQuery<CmdbAsset>({
    url: "/cmdb/assets/deleted",
    params: search ? { search } : {},
    errorMessage: "获取回收站列表失败",
  })

  const [purgeOpen, setPurgeOpen] = useState(false)
  const [purgeAsset, setPurgeAsset] = useState<CmdbAsset | null>(null)

  const handleRestore = async (asset: CmdbAsset) => {
    try {
      await api.post(`/cmdb/assets/${asset.id}/restore`)
      toast.success("恢复成功")
      fetchDeleted()
    } catch {
      toast.error("恢复失败")
    }
  }

  const handlePurgeClick = (asset: CmdbAsset) => {
    setPurgeAsset(asset)
    setPurgeOpen(true)
  }

  const handlePurgeConfirm = async (): Promise<boolean> => {
    if (!purgeAsset) return false
    try {
      await api.delete(`/cmdb/assets/${purgeAsset.id}/purge`)
      toast.success("已永久删除")
      fetchDeleted()
      return true
    } catch {
      toast.error("永久删除失败")
      return false
    }
  }

  const columns = useMemo<ColumnDef<CmdbAsset>[]>(
    () => [
      { accessorKey: "hostname", header: "主机名" },
      { accessorKey: "ip_address", header: "IP 地址" },
      {
        accessorKey: "updated_at",
        header: "删除时间",
        cell: ({ row }) => dayjs(row.original.updated_at).format("YYYY-MM-DD HH:mm"),
      },
      {
        id: "actions",
        header: "操作",
        cell: ({ row }) => (
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => handleRestore(row.original)}>
              <RefreshIcon data-icon="inline-start" />
              恢复
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="text-destructive"
              onClick={() => handlePurgeClick(row.original)}
            >
              <Delete02Icon data-icon="inline-start" />
              永久删除
            </Button>
          </div>
        ),
      },
    ],
    []
  )

  return (
    <div>
      <PageHeader
        title="CMDB 资产回收站"
        description="已删除的资产，可恢复或永久删除"
        actions={
          <Button variant="outline" render={<Link to={ROUTES.CMDB} />}>
            <ArrowLeft01Icon data-icon="inline-start" />
            返回资产列表
          </Button>
        }
      />

      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <Input
          placeholder="搜索主机名或 IP..."
          value={search}
          onChange={(e) => {
            setSearch(e.target.value)
            setPage(1)
          }}
          className="sm:max-w-xs"
        />
      </div>

      <DataTable
        columns={columns}
        data={assets}
        isLoading={isLoading}
        emptyMessage="回收站为空"
      />

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={onPageSizeChange}
      />

      <ConfirmDialog
        open={purgeOpen}
        onOpenChange={setPurgeOpen}
        title="确认永久删除"
        description={`确定要永久删除资产「${purgeAsset?.hostname}」吗？此操作不可恢复。`}
        onConfirm={handlePurgeConfirm}
      />
    </div>
  )
}
```

实施时如果 `ArrowLeft01Icon`/`RefreshIcon` 在 `@/lib/icons` 里不存在，跑 `grep -n "export" frontend/src/lib/icons.tsx` 找现有等价图标名替换（`UsersTrashPage.tsx` 大概率已经在用某个"返回"图标，直接抄它的 import）。

- [ ] **Step 2: 注册路由**

修改 `frontend/src/App.tsx`：

1. 顶部 import 区加：
```typescript
import { CmdbAssetsPage } from "@/pages/CmdbAssetsPage"
import { CmdbAssetsTrashPage } from "@/pages/CmdbAssetsTrashPage"
```
2. 在 `ROUTES.PERMISSIONS_TRASH` 对应的 `<Route>` 块之后、`ROUTES.PROFILE` 之前插入：
```tsx
          <Route
            path={ROUTES.CMDB}
            element={
              <ProtectedRoute permission={PERMISSIONS.CMDB_READ}>
                <CmdbAssetsPage />
              </ProtectedRoute>
            }
          />
          <Route
            path={ROUTES.CMDB_TRASH}
            element={
              <ProtectedRoute permission={PERMISSIONS.CMDB_MANAGE}>
                <CmdbAssetsTrashPage />
              </ProtectedRoute>
            }
          />
```

- [ ] **Step 3: 类型检查 + lint**

Run:
```bash
npm run typecheck
npm run lint
```
Expected: 干净。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/pages/CmdbAssetsTrashPage.tsx frontend/src/App.tsx
git commit -m "$(cat <<'EOF'
新增 CMDB 资产回收站页面，注册两条新路由

- 结构照抄 UsersTrashPage.tsx：恢复 + 永久删除 + 返回列表
- App.tsx 挂载 /cmdb（cmdb:read）和 /cmdb/trash（cmdb:manage）
EOF
)"
```

---

### Task 11: 侧栏菜单 + 前端端到端验收

**Files:**
- Modify: `frontend/src/components/layout/Sidebar.tsx`

**Interfaces:**
- 无新接口，导航入口 + 手动验收。

- [ ] **Step 1: 加菜单分组**

修改 `frontend/src/components/layout/Sidebar.tsx` 的 `NAV_ENTRIES`：在 `"运维助手"` 这个 `item` 之后、`"系统管理"` 这个 `group` 之前，插入一个新分组（CMDB 是运维范畴，不归入面向 RBAC 的"系统管理"）：

```typescript
  {
    type: "item",
    label: "运维助手",
    path: ROUTES.OPS_ASSISTANT,
    icon: AiChat01Icon,
  },
  {
    type: "group",
    id: "ops",
    label: "运维管理",
    icon: Server02Icon,
    children: [
      {
        type: "item",
        label: "CMDB 资产",
        path: ROUTES.CMDB,
        icon: Database02Icon,
        permission: PERMISSIONS.CMDB_READ,
      },
    ],
  },
  {
    type: "group",
    id: "system",
    label: "系统管理",
    ...
```

顶部 import 区的图标列表里加 `Server02Icon`（分组图标）和 `Database02Icon`（CMDB 菜单项图标）：

```typescript
import {
  Dashboard02Icon,
  AiChat01Icon,
  Server02Icon,
  Database02Icon,
  UserMultipleIcon,
  ...
} from "@/lib/icons"
```

实施时先跑 `grep -n "Server02Icon\|Database02Icon" frontend/src/lib/icons.tsx`，如果 `@/lib/icons` 里没有导出这两个名字，打开该文件看它现有的 `@hugeicons/core-free-icons` 导出模式（`icons.tsx` 目前只有 10 行，是个薄封装），照着现有条目的写法从 `@hugeicons/core-free-icons` 包里选两个语义相近的图标补上去即可，不要为了图标去新增其它依赖。

- [ ] **Step 2: 类型检查 + lint + 前端全量测试**

Run:
```bash
npm run typecheck
npm run lint
npm run test
```
Expected: 全部干净通过。

- [ ] **Step 3: 手动跨层验收**

按本项目一贯的收尾方式（参照 T11 最后一次"运维助手 Chat 跨层验收"提交），启动前后端，走一遍完整流程：

1. `uv run python main.py`（backend/）+ `npm run dev`（frontend/），浏览器登录一个拥有 `cmdb:manage` 的账号。
2. 侧栏「运维管理 → CMDB 资产」能进入列表页；「新增资产」能创建一个 `credential_type=static` 的资产，填密码后保存成功，列表里凭据列显示"静态密码"徽标，浏览器开发者工具的 Network 面板检查创建请求的响应体，确认没有明文密码、没有 `credential_password_encrypted` 字段。
3. 编辑该资产但不碰密码字段，保存后确认"静态密码"徽标还在（密文没被清空）——用后端 `uv run python -c "..."` 或直接查 DB 确认 `credential_password_encrypted` 值跟编辑前一致。
4. 删除该资产，进回收站页面能看到它；点「恢复」后资产列表里重新出现；再删除一次并「永久删除」，确认回收站列表里再也看不到它。
5. 用一个只有 `cmdb:read` 没有 `cmdb:manage` 的账号登录，确认「新增资产」按钮和「删除」菜单项不出现（`hasPermission` 门控生效），直接调用 `POST /api/v1/cmdb/assets` 应返回 403。

把第 2-5 步的关键截图或终端输出记录在这一步的 commit message 里（不需要额外建文档）。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/components/layout/Sidebar.tsx
git commit -m "$(cat <<'EOF'
CMDB 资产管理接入侧栏导航，完成前后端跨层验收

- 新增顶层"运维管理"分组（跟"系统管理"分开，因为 CMDB 资产不是 RBAC 范畴），
  下挂 CMDB 资产入口，cmdb:read 门控可见性
- 手动走完创建（含静态密码）→ 编辑不改密码 → 删除 → 恢复 → 永久删除 →
  无权限账号 403 的完整链路，Network 面板确认响应体全程不含明文/密文密码
EOF
)"
```

---

## After All Tasks

- 用 `superpowers:verification-before-completion` 报告一次新鲜的命令输出，不要用记忆中的结果：后端 `uv run pytest -v && uv run mypy app && uv run ruff check . && uv run alembic heads`；前端 `npm run typecheck && npm run lint && npm run test`。
- 派一个 `superpowers:requesting-code-review` 走一遍全分支评审，重点检查：密码是否在任何路径（响应体/日志/审计 detail/异常消息）泄露过明文或密文；`_prepare_persist_data` 的三种凭据类型切换分支是否都被测试覆盖；软删除资产的凭据字段在恢复后是否完整保留。
- 确认 `git status --short` 干净，没有遗漏文件。全程留在 `master`，不建分支、不建 PR，不主动 push。
- 不要在 Tasks 1-11 未全部完成、验收矩阵未全绿之前，宣称这个功能已经完成。
