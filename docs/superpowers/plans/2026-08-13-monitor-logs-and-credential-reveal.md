# 监控日志与静态凭据查看 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 巡检只把状态变化留成可查的监控日志（默认保留 7 天可配），侧栏新增「日志管理」；有 `cmdb:credential_read` 的人能按需查看静态密码明文。

**Architecture:** 继续用 `monitor_status_events`：状态未变则更新当前行时间，变了才插入。保留天数走现有 `system_configs`。监控日志列表用新权限 `monitor_log:read`。静态密码用现有 Fernet 解密，单独 GET，明文不进资产详情。

**Tech Stack:** Python 3.14.3、FastAPI、SQLAlchemy 2 async、Pydantic v2、React 19、TypeScript、shadcn/Base UI、pytest、Vitest。`uv run` / `pnpm`。

**Spec:** `docs/superpowers/specs/2026-08-13-monitor-logs-and-credential-reveal-design.md`

## Global Constraints

- 只在 `master` 工作，不建分支；每个任务验证通过后按项目规范提交一次，禁止 `Co-Authored-By`，未要求不要 push。
- 后端在 `backend/` 下用 `uv run`；前端在 `frontend/` 下用 `pnpm`。不新增依赖。
- 不编辑真实 `backend/.env`，只改 `.env.example`。
- 不新建监控历史表，不做 Alembic 新表迁移。
- 新权限码固定为 `monitor_log:read` 与 `cmdb:credential_read`，并写入 `init_db.py` 的 `SEED_PERMISSIONS`。
- `GET /monitor/logs` 只要 `monitor_log:read`，不要用 `monitor:read` 代替。
- 资产列表/详情响应不得出现密码明文或密文。
- 查看密码的审计 `action` 为 `view_cmdb_credential`，`detail` 不含明文/密文。
- 保留天数键名 `MONITOR_EVENT_RETENTION_DAYS`，整数 1–90，默认 7。
- Python 文件头注释、中文文档字符串按项目规则；图标只从 `@/lib/icons` 引入。
- 表单用 `FieldGroup` + `Field`；侧栏分组用现有 `NavGroup`。

---

## File Structure

| 文件 | 职责 |
| :--- | :--- |
| `backend/app/crud/monitor_status_event.py` | `record_probe`（同状态更新/变状态插入）、`purge_older_than`、`list_logs` |
| `backend/app/services/monitor_sweep.py` | 调用 `record_probe` 与清理 |
| `backend/app/services/system_config.py` 等 | 运行配置增加保留天数 |
| `backend/init_db.py` | 种子：两权限 + 保留天数键 |
| `backend/app/api/v1/monitor.py` | `GET /logs` |
| `backend/app/api/v1/cmdb.py` | `GET /assets/{id}/credential` |
| `frontend/src/pages/MonitorLogsPage.tsx` | 监控日志页 |
| `frontend/src/components/layout/Sidebar.tsx` | 「日志管理」分组 |
| `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx` | 查看密码 |

---

### Task 1: 同状态更新、变状态插入

**Files:**
- Modify: `backend/app/crud/monitor_status_event.py`
- Modify: `backend/app/services/monitor_sweep.py`
- Test: `backend/tests/test_monitor_sweep.py`
- Test: `backend/tests/test_monitor_crud_status_event.py`（若已有则追加，没有就只写 sweep 测试）

**Interfaces:**
- Produces: `CRUDMonitorStatusEvent.record_probe(db, *, target_id: int, status: str, latency_ms: int | None = None, detail: str = "") -> MonitorStatusEvent`
- `run_monitor_sweep_once` 改为调用 `record_probe` 而不是 `record`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_monitor_sweep.py` 追加：

```python
async def test_second_sweep_same_status_updates_existing_event(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = await monitor_target_crud.create(
        db_session,
        {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "is_active": True},
    )
    await db_session.commit()

    async def fake_probe(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]:
        return "up", 5, ""

    monkeypatch.setattr("app.services.monitor_sweep.probe_tcp", fake_probe)
    await run_monitor_sweep_once(db_session)
    first = (await monitor_status_event_crud.list_recent_for_target(db_session, target.id))[0]
    first_id = first.id
    first_checked = first.checked_at

    await run_monitor_sweep_once(db_session)
    events = await monitor_status_event_crud.list_recent_for_target(db_session, target.id)
    assert len(events) == 1
    assert events[0].id == first_id
    assert events[0].checked_at >= first_checked


async def test_status_change_inserts_new_event(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = await monitor_target_crud.create(
        db_session,
        {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "is_active": True},
    )
    await db_session.commit()
    statuses = iter([("up", 3, ""), ("down", None, "连接超时")])

    async def fake_probe(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]:
        return next(statuses)

    monkeypatch.setattr("app.services.monitor_sweep.probe_tcp", fake_probe)
    await run_monitor_sweep_once(db_session)
    await run_monitor_sweep_once(db_session)
    events = await monitor_status_event_crud.list_recent_for_target(db_session, target.id)
    assert [item.status for item in events] == ["down", "up"]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
uv run pytest tests/test_monitor_sweep.py::test_second_sweep_same_status_updates_existing_event tests/test_monitor_sweep.py::test_status_change_inserts_new_event -q
```

Expected: FAIL（第二轮仍 INSERT，行数变成 2）

- [ ] **Step 3: 实现 `record_probe` 并改 sweep**

`record_probe`：用 `get_latest_status_for_targets(db, [target_id])` 取当前行。若存在且 `status` 相同，更新 `checked_at=datetime.now(UTC)`、`latency_ms`、`detail` 后 `flush`；否则走现有 `record()`。

`run_monitor_sweep_once` 里把 `monitor_status_event_crud.record(...)` 换成 `record_probe(...)`。

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_monitor_sweep.py tests/test_monitor_crud_status_event.py -q
```

Expected: PASS（含原有「每台启用目标一条」用例）

- [ ] **Step 5: Commit**

```text
巡检改为只在状态变化时追加监控事件

- 同一状态更新当前行的探测时间，避免日志被「一直在线」刷满
- 在线/离线切换才插入新行，供后续监控日志页查询突然断开
```

---

### Task 2: 保留天数配置落库

**Files:**
- Modify: `backend/app/core/config.py`（`MONITOR_EVENT_RETENTION_DAYS: int = Field(default=7, ge=1, le=90)`）
- Modify: `backend/.env.example`（`MONITOR_EVENT_RETENTION_DAYS=7`）
- Modify: `backend/app/services/system_config.py`（键、`OPERATIONS_CONFIG_KEYS`、`EffectiveOperationsConfig`、读写）
- Modify: `backend/app/schemas/system_config.py`
- Modify: `backend/app/api/v1/system_config.py`（PUT 映射该键）
- Modify: `backend/init_db.py`（`_system_config_seed_values`）
- Modify: `backend/tests/test_system_config_seeds.py`（四项改为五项）
- Modify: `backend/tests/test_system_config_api.py`（`_operations_payload` 加字段）
- Modify: `frontend/src/types/system-config.ts`
- Modify: `frontend/src/components/system-config/systemConfigFormSchemas.ts`
- Modify: `frontend/src/components/system-config/systemConfigFormSchemas.test.ts`
- Modify: `frontend/src/components/system-config/OperationsConfigCard.tsx`

**Interfaces:**
- Produces: `EffectiveOperationsConfig.monitor_event_retention_days: int`
- `OperationsSystemConfigUpdate.monitor_event_retention_days: int = Field(ge=1, le=90)`

- [ ] **Step 1: 改种子测试为期望 5 个运行键**

`test_seed_system_configs_creates_only_four_operational_keys` 改名为 `...five...`，断言 `== 5`，并 `assert "MONITOR_EVENT_RETENTION_DAYS" in OPERATIONS_CONFIG_KEYS`。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_system_config_seeds.py -q
```

Expected: FAIL（仍是 4 个键）

- [ ] **Step 3: 实现配置键贯通**

- 常量 `KEY_MONITOR_EVENT_RETENTION_DAYS = "MONITOR_EVENT_RETENTION_DAYS"` 加入 `OPERATIONS_CONFIG_KEYS`
- 增加 `_resolve_int_value`（或 float 后再 `int`），校验走 `OperationsSystemConfigUpdate`
- `save_operations_config` / `build_system_config_response` / `init_db._system_config_seed_values` 写入该键
- 前端 `operationsConfigFormSchema` 增加 `monitor_event_retention_days: z.coerce.number().int().min(1).max(90)`
- `validOperationsForm` 与 `_operations_payload` 补默认 `7`
- `OperationsConfigCard` 增加「监控日志保留天数」输入，说明：过期变化记录会被清理，每台最新一条会保留

- [ ] **Step 4: 验证**

```bash
uv run pytest tests/test_system_config_seeds.py tests/test_system_config_api.py tests/test_system_config_service.py -q
cd ../frontend
pnpm exec vitest run src/components/system-config/systemConfigFormSchemas.test.ts
```

Expected: PASS

- [ ] **Step 5: Commit**

```text
系统配置增加监控日志保留天数（默认 7 天）

- 键 MONITOR_EVENT_RETENTION_DAYS 落库，范围 1–90，.env 作回退
- 运行参数表单可改该值，init_db 幂等种子写入
```

---

### Task 3: 按保留天数清理过期事件

**Files:**
- Modify: `backend/app/crud/monitor_status_event.py`（`purge_older_than`）
- Modify: `backend/app/services/monitor_sweep.py`（本轮写入后、commit 前调用）
- Test: `backend/tests/test_monitor_sweep.py` 或 `backend/tests/test_monitor_crud_status_event.py`

**Interfaces:**
- Consumes: `EffectiveOperationsConfig.monitor_event_retention_days`
- Produces: `purge_older_than(db, *, retention_days: int) -> int`（删除行数）

- [ ] **Step 1: 写失败测试**

造一台目标、插入两行事件：旧的 `down`（`checked_at` 设为 10 天前）、新的 `up`（现在）。调用 `purge_older_than(db, retention_days=7)`。断言只剩 `up` 那一行。

再造一台目标只有一行且 `checked_at` 为 10 天前，清理后这一行仍在。

- [ ] **Step 2: 跑测试确认失败**

Expected: FAIL（方法不存在或未删除）

- [ ] **Step 3: 实现清理**

用 `row_number()` 按 `target_id` 分区、`checked_at desc, id desc` 排序。删除 `rn > 1` 且 `checked_at < now - retention_days` 的行。不要删 `rn == 1`。

`run_monitor_sweep_once` 在记录探测之后读取 `monitor_event_retention_days` 再 purge，然后 `commit`。

- [ ] **Step 4: 跑测试**

```bash
uv run pytest tests/test_monitor_sweep.py tests/test_monitor_crud_status_event.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```text
按配置的保留天数清理过期监控事件

- 删除超过 N 天的历史变化行，每台目标最新一行即使过期也保留
- 避免卡片因清理丢失当前在线状态
```

---

### Task 4: 监控日志 API 与 `monitor_log:read` 种子

**Files:**
- Modify: `backend/init_db.py`（`SEED_PERMISSIONS` 增加监控日志权限）
- Modify: `backend/tests/test_hitl_permission_seeds.py`（REQUIRED 加入 `monitor_log:read`）
- Modify: `backend/app/schemas/monitor.py`（`MonitorLogItem`）
- Modify: `backend/app/crud/monitor_status_event.py`（`list_logs`）
- Modify: `backend/app/api/v1/monitor.py`（`GET /logs`）
- Test: `backend/tests/test_monitor_api.py`

**Interfaces:**
- Produces: `GET /api/v1/monitor/logs`，权限 `monitor_log:read`
- `MonitorLogItem`: `id, target_id, label, ip_address, port, status, latency_ms, detail, checked_at`

权限种子：

```python
{
    "name": "查看监控日志",
    "code": "monitor_log:read",
    "module": "监控",
    "description": "查看监控探活状态变化历史",
},
```

`list_logs`：`select` 事件 JOIN `monitor_targets`，可选 `target_id`、`status in {up,down}`、`search` 对 `label`/`ip_address` ilike，按 `checked_at desc, id desc`，返回 `(items, total)`。

路由必须写在 `/targets/{id}` 之前或使用静态路径 `/logs`，避免被当成 target_id。现有文件已有 `/runtime`，把 `/logs` 放在 `/targets` 旁边即可。

- [ ] **Step 1: 种子测试加入 `monitor_log:read`，API 测试：无权限 403、有权限能按 target_id 筛到 down 行**

授权辅助函数可仿 `_grant_monitor_permissions`，增加 `monitor_log:read`。有 `monitor:read` 没有 `monitor_log:read` 时 `/monitor/logs` 必须 403。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_hitl_permission_seeds.py tests/test_monitor_api.py -q
```

Expected: FAIL

- [ ] **Step 3: 实现种子、schema、CRUD、路由**

- [ ] **Step 4: 跑测试**

```bash
uv run pytest tests/test_hitl_permission_seeds.py tests/test_monitor_api.py tests/test_monitor_sweep.py -q
uv run ruff check app/crud/monitor_status_event.py app/api/v1/monitor.py init_db.py
uv run mypy app/crud/monitor_status_event.py app/api/v1/monitor.py app/schemas/monitor.py
```

Expected: PASS

- [ ] **Step 5: Commit**

```text
新增监控日志列表接口与 monitor_log:read 权限

- GET /monitor/logs 分页筛选状态变化记录，与监控目标管理权限分离
- init_db 种子写入该权限码
```

---

### Task 5: 「日志管理」菜单与监控日志页

**Files:**
- Modify: `frontend/src/lib/constants.ts`（`ROUTES.MONITOR_LOGS = "/monitor-logs"`，`PERMISSIONS.MONITOR_LOG_READ = "monitor_log:read"`）
- Modify: `frontend/src/components/layout/Sidebar.tsx`
- Modify: `frontend/src/components/layout/Header.tsx`
- Modify: `frontend/src/App.tsx`
- Create: `frontend/src/pages/MonitorLogsPage.tsx`
- Modify: `frontend/src/types/monitor.ts`（`MonitorLogItem`）
- Modify: `frontend/src/pages/MonitorTargetsPage.tsx`（有权限时「查看日志」）

**Interfaces:**
- Consumes: `GET /api/v1/monitor/logs`
- 侧栏结构：

```text
日志管理（分组 id: "logs"，图标 FileEditIcon 或 AuditIcon）
  监控日志 → ROUTES.MONITOR_LOGS，permission MONITOR_LOG_READ
  操作日志 → ROUTES.AUDIT，permission AUDIT_READ
```

删掉原来顶层的「操作日志」单项。

监控日志页照 `AuditLogsPage`：`PageHeader`、搜索 IP/标签、`Select` 状态（全部/在线/离线，`SelectItem` 包在 `SelectGroup`）、`DataTable`、`Pagination`。`usePaginatedQuery` 的 url 为 `/monitor/logs`。用 `useSearchParams` 读 `target_id`。

监控目标卡片下拉：`hasPermission(PERMISSIONS.MONITOR_LOG_READ)` 时增加「查看日志」，`navigate(\`${ROUTES.MONITOR_LOGS}?target_id=${target.id}\`)`。

面包屑：

```ts
[ROUTES.MONITOR_LOGS]: [{ label: "日志管理" }, { label: "监控日志" }],
[ROUTES.AUDIT]: [{ label: "日志管理" }, { label: "操作日志" }],
```

- [ ] **Step 1: 实现页面与导航（本页无现成单测框架，用类型检查验收）**

- [ ] **Step 2: 验证**

```bash
cd frontend
pnpm exec tsc -b --pretty false --force
```

Expected: 无错误

- [ ] **Step 3: Commit**

```text
新增日志管理菜单与监控日志页

- 侧栏独立「日志管理」分组，下挂监控日志与原操作日志
- 监控日志页按设备/状态筛选，卡片可跳转到该目标的历史
```

---

### Task 6: 查看静态密码 API 与 `cmdb:credential_read` 种子

**Files:**
- Modify: `backend/init_db.py`
- Modify: `backend/tests/test_hitl_permission_seeds.py`（REQUIRED 加入 `cmdb:credential_read`）
- Modify: `backend/app/schemas/cmdb.py`（`CmdbCredentialRevealResponse`）
- Modify: `backend/app/api/v1/cmdb.py`
- Test: `backend/tests/test_cmdb_api.py`

**Interfaces:**
- Produces: `GET /api/v1/cmdb/assets/{id}/credential` → `{ password: str }`
- 权限：`cmdb:credential_read`
- 审计：`action="view_cmdb_credential"`，`target=f"cmdb_asset:{id}"`，detail 含 hostname、不含密码

种子：

```python
{
    "name": "查看 CMDB 静态凭据",
    "code": "cmdb:credential_read",
    "module": "CMDB",
    "description": "查看已保存的静态登录密码明文",
},
```

路由逻辑：

1. `cmdb_asset_crud.get`，没有或已删 → 404
2. 不是 static 或密文为空 → 422「该资产没有可查看的静态密码」
3. `decrypt_credential_password`；缺密钥 / 解密失败 → 503，中文消息与现有保存凭据失败一致
4. `log_audit` 后返回明文

- [ ] **Step 1: 写 API 测试**

- 只有 `cmdb:read` → 403
- 静态密码资产 + `cmdb:credential_read` → 200 且 `data.password` 等于加密前明文
- 同一请求后审计表有 `view_cmdb_credential`，`detail` 不含明文
- GET 资产详情仍无 `password` 字段
- `credential_type=none` → 422

测试里用 `Fernet.generate_key()` monkeypatch `CMDB_CREDENTIAL_KEY`，与现有 cmdb 测试相同。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_cmdb_api.py tests/test_hitl_permission_seeds.py -q
```

- [ ] **Step 3: 实现**

- [ ] **Step 4: 跑测试 + ruff + mypy**

Expected: PASS

- [ ] **Step 5: Commit**

```text
新增查看 CMDB 静态凭据接口与独立权限

- GET /cmdb/assets/{id}/credential 按需解密，详情接口仍不返回明文
- 查看行为写入审计且不含密码；init_db 种子增加 cmdb:credential_read
```

---

### Task 7: 前端查看密码与操作日志文案

**Files:**
- Modify: `frontend/src/lib/constants.ts`（`PERMISSIONS.CMDB_CREDENTIAL_READ`）
- Modify: `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx`
- Modify: `frontend/src/pages/CmdbAssetsPage.tsx`（可选：操作菜单「查看密码」）
- Modify: `frontend/src/pages/AuditLogsPage.tsx`（`ACTION_LABELS.view_cmdb_credential = "查看资产凭据"`）

**Interfaces:**
- Consumes: `GET /cmdb/assets/{id}/credential`
- Dialog：`DialogTitle` 必填；密码用 `InputGroup` + 显示/隐藏，不要写回表单 `credential_password` 字段

仅当 `isEdit && asset.credential_type === "static" && asset.credential_password_set && hasPermission(CMDB_CREDENTIAL_READ)` 显示「查看密码」。点击后请求接口，成功用 Dialog 展示，失败 toast。

- [ ] **Step 1: 实现 UI**

- [ ] **Step 2: 验证**

```bash
cd frontend
pnpm exec tsc -b --pretty false --force
pnpm exec vitest run src/components/cmdb/CmdbAssetFormDialog.test.tsx
```

Expected: PASS

- [ ] **Step 3: Commit**

```text
资产编辑支持按权限查看已保存的静态密码

- 有 cmdb:credential_read 时可解密查看，明文不进入提交表单
- 操作日志动作文案补上「查看资产凭据」
```

---

## Spec coverage

| 规格 | 任务 |
| :--- | :--- |
| 同状态更新 / 变状态插入 | Task 1 |
| 保留天数配置与种子 | Task 2 |
| 过期清理且保留最新行 | Task 3 |
| `GET /monitor/logs` + `monitor_log:read` | Task 4 |
| 日志管理菜单 + 监控日志页 + 卡片跳转 | Task 5 |
| 查看密码 API + `cmdb:credential_read` + 审计 | Task 6 |
| 查看密码 UI | Task 7 |
| 详情不返回明文 | Task 6 测试约束 |
| 不新建表 | 全任务 |

## 本地验收（全部任务完成后）

```bash
cd backend
uv run python init_db.py
uv run pytest tests/test_monitor_sweep.py tests/test_monitor_api.py tests/test_cmdb_api.py tests/test_system_config_seeds.py tests/test_hitl_permission_seeds.py -q
cd ../frontend
pnpm exec tsc -b --pretty false --force
```

超级管理员刷新后应看到「日志管理」。给普通角色勾选 `monitor_log:read` / `cmdb:credential_read` 后分别能进监控日志、能看静态密码。
