# 监控日志与静态凭据查看 Design Spec

**状态**：已与项目所有者确认设计（2026-08-13），含菜单与权限修订。

## 1. 目标与背景

两件独立但同一次交付的事：

1. **监控日志**：卡片只能看到最新一次探测，无法回看「突然断开」。探测结果其实已经写在 `monitor_status_events`，缺的是「只保留状态变化、按天数清理、单独页面查看」。
2. **查看静态密码**：CMDB 静态凭据用 `.env` 的 `CMDB_CREDENTIAL_KEY` 做 Fernet 对称加密。列表/详情只返回 `credential_password_set`，保存后再也看不到明文。超级管理员和持有新权限的人需要能按需解密查看。

## 2. 复用点（不重新发明）

| 复用对象 | 现状 | 本设计的用法 |
| :--- | :--- | :--- |
| `monitor_status_events` | 每次巡检 INSERT 一行；当前状态 = 最新一行 | 状态没变则 UPDATE 最新行的时间/延迟；变了才 INSERT。不新建表 |
| `list_recent_for_target` | CRUD 已有、尚未对外 | 扩展成分页+筛选列表，给监控日志页用 |
| 系统配置 `system_configs` | 运行参数已落库，`.env` 作回退 | 新增键 `MONITOR_EVENT_RETENTION_DAYS`，默认 7 |
| `run_monitor_sweep_once` | 每轮探测后 commit | 写入改为「变化才插行」，commit 前按保留天数清理过期行 |
| `encrypt/decrypt_credential_password` | 执行设备命令时已解密 | 查看密码接口复用，不另做一套加密 |
| `require_permission` / 超级管理员放行 | 现有 RBAC | 新权限 `monitor_log:read`、`cmdb:credential_read` |
| `init_db.SEED_PERMISSIONS` / `seed_system_configs` | 幂等补权限与运行配置 | 种子增加两个权限码和保留天数键 |
| 侧栏分组 `NavGroup` | 已有「运维管理」「系统管理」 | 新增「日志管理」，把现有「操作日志」挪进去 |
| `AuditLogsPage` + `usePaginatedQuery` | 操作日志表格页 | 监控日志页照这个结构抄，不新造列表框架 |
| `log_audit` | 写操作与审计同一事务 | 查看密码必须记审计，明文不进审计 |

## 3. 监控：只记状态变化

### 3.1 写入规则

巡检对每台启用目标探测一次后：

1. 该目标还没有任何事件 → **INSERT**（第一次探测）。
2. 最新事件的 `status` 与本次相同 → **UPDATE** 该行的 `checked_at`、`latency_ms`、`detail`。卡片上的「最近探测」仍会刷新，但日志里不会堆「一直在线」的重复行。
3. `status` 从 `up` 变成 `down` 或反过来 → **INSERT** 新行。这就是「突然断开 / 恢复」的历史。

不新增第二张日志表。当前在线状态仍然只从最新一行推导，符合「同一事实不要两份数据」。

### 3.2 保留天数

- 配置键：`MONITOR_EVENT_RETENTION_DAYS`
- 类型：整数，范围 **1–90**，默认 **7**
- 来源优先级与其他运行参数相同：数据库有该键用数据库值，否则回退 `.env` / `Settings`
- `init_db.py` 的 `_system_config_seed_values()` 幂等写入该键（初始值取当前 Settings，未配环境变量则为 `7`）
- 系统配置页「运行参数」增加「监控日志保留天数」

清理时机：每轮 `run_monitor_sweep_once` 写完本轮探测、`commit` 之前删除过期行。

清理规则：

```text
DELETE 满足：
  checked_at < now() - retention_days
  AND id 不是「每台目标最新那一行」
```

长期一直在线的设备，最新那一行即使超过 7 天也必须留下，否则卡片会变成「未探测」。

### 3.3 列表 API

```text
GET /api/v1/monitor/logs
权限：monitor_log:read（超级管理员自动放行）
```

查询参数：

| 参数 | 说明 |
| :--- | :--- |
| `page` / `page_size` | 与现有分页一致 |
| `target_id` | 可选，只看某一台监控目标 |
| `status` | 可选，`up` 或 `down` |
| `search` | 可选，匹配目标标签或 IP |

响应每条包含：`id`、`target_id`、`label`、`ip_address`、`port`、`status`、`latency_ms`、`detail`、`checked_at`。按 `checked_at`、`id` 倒序。

日志只读，不提供删除接口。删除监控目标时仍 CASCADE 删掉该目标事件。

`monitor:read` **不能**调这个接口：能看卡片的人不一定能翻历史。

### 3.4 前端：日志管理大菜单

侧栏**不要**把监控日志放在「运维管理」下。新增分组：

```text
日志管理
  ├─ 监控日志   /monitor-logs     权限 monitor_log:read
  └─ 操作日志   /audit            权限 audit:read（现有页，只改菜单位置）
```

分组对用户可见的条件：至少拥有其中一个子项权限（现有 `Sidebar` 已按子项过滤）。

面包屑：`日志管理 / 监控日志`、`日志管理 / 操作日志`。

监控日志页：表格 + 设备/状态/关键词筛选 + 分页。监控目标卡片在持有 `monitor_log:read` 时提供「查看日志」，跳到 `/monitor-logs?target_id={id}`。

## 4. 静态凭据查看

### 4.1 权限

```text
code: cmdb:credential_read
name: 查看 CMDB 静态凭据
module: CMDB
description: 查看已保存的静态登录密码明文
```

写入 `SEED_PERMISSIONS`。`cmdb:read` / `cmdb:manage` **不能**看明文。超级管理员走现有放行。普通角色要在「权限管理」里勾选该码。

### 4.2 API

列表、详情、编辑响应**继续**只返回 `credential_password_set`，不带明文。

```text
GET /api/v1/cmdb/assets/{id}/credential
权限：cmdb:credential_read
```

成功：`{ "password": "<明文>" }`，仅当 `credential_type == "static"` 且密文字段非空。

失败：

- 资产不存在或已软删 → 404
- 非静态凭据，或未设置密码 → 422，中文说明「该资产没有可查看的静态密码」
- 未配置 `CMDB_CREDENTIAL_KEY` → 503（与现有保存凭据失败语义一致）
- 密文无法解密 → 503，提示密钥可能已轮换

同事务写审计：

- `action`: `view_cmdb_credential`
- `target`: `cmdb_asset:{id}`
- `detail`: 可含 hostname，**禁止**含明文或密文

动态凭据不存密码，不提供查看。

### 4.3 前端

编辑弹窗里，静态密码且「已设置」、且当前用户有 `cmdb:credential_read` 时，显示「查看密码」。点击后调上述接口，用 Dialog 展示（默认隐藏字符，可切换显示）。不要把明文写回编辑表单的提交字段，避免「看一眼变成改密码」。

资产列表操作菜单同样可放「查看密码」，权限与条件相同。

操作日志页的动作字典补上 `view_cmdb_credential` → 「查看资产凭据」。

## 5. 种子与配置回退

`backend/init_db.py`：

- `SEED_PERMISSIONS` 增加 `monitor_log:read`、`cmdb:credential_read`
- `_system_config_seed_values()` 增加 `MONITOR_EVENT_RETENTION_DAYS`

`backend/app/core/config.py` 与 `backend/.env.example` 增加 `MONITOR_EVENT_RETENTION_DAYS=7` 作为数据库未配置时的回退。不把密钥或明文写进种子。

已有库执行 `uv run python init_db.py` 即可补权限行和配置行（幂等，已存在则跳过）。

## 6. 不在本次范围

- 不为监控日志做图表/延迟曲线（只记变化，没有连续采样）
- 不新建第二张历史表，不做 Alembic 新表迁移
- 不把明文密码放进资产 GET/PATCH 响应
- 不提供「复制密码到剪贴板」以外的导出；若做复制，也不得写入审计明文
- 不改动态凭据流程
- 不把监控日志权限并入 `monitor:read`

## 7. 验收标准

1. 同一台设备连续多次在线探测，事件表只有 1 行，但 `checked_at` 会更新。
2. 在线→离线会产生新的 `down` 行；监控日志页能按设备筛到这条。
3. 系统配置可改保留天数；超过天数的旧变化行被清掉，每台最新一行仍在。
4. 无 `monitor_log:read` 的账号打不开监控日志页，也不能调 `GET /monitor/logs`。
5. 侧栏出现「日志管理」，其下是监控日志与操作日志；操作日志不再作为顶层单项。
6. 无 `cmdb:credential_read` 时看不到「查看密码」，详情里仍然没有密码字段。
7. 有权限时能看到静态密码明文；审计有「查看资产凭据」且不含密码。
8. `init_db.py` 种子包含上述两个权限码和保留天数键。
