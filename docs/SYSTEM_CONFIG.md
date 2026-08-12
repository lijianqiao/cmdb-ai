# 系统运行配置

本文档说明 12 个受管运行参数的存储、优先级、权限、生效时机、部署升级与安全轮换。完整实现见 `backend/app/services/system_config.py` 与前端 `系统配置` 页面（`/system-config`）。

---

## 1. 受管键清单与 UI 字段映射

系统仅允许以下 12 个键写入 `system_configs` 表；API 与 UI 均通过白名单校验，无法创建任意未知键。

| 数据库键 | UI 分组 | UI 字段标签 | 类型 |
| --- | --- | --- | --- |
| `LLM_CHAT_BASE_URL` | 模型配置 → Chat | Base URL | 字符串（URL） |
| `LLM_CHAT_MODEL` | 模型配置 → Chat | 模型名 | 字符串 |
| `LLM_CHAT_INPUT_COST_PER_MILLION_USD` | 模型配置 → Chat | 输入价格 / 百万 tokens（USD） | 浮点数 ≥ 0 |
| `LLM_CHAT_OUTPUT_COST_PER_MILLION_USD` | 模型配置 → Chat | 输出价格 / 百万 tokens（USD） | 浮点数 ≥ 0 |
| `LLM_CHAT_API_KEY` | 模型配置 → Chat | API Key | 秘密（Fernet 密文入库） |
| `LLM_EMBEDDING_BASE_URL` | 模型配置 → Embedding | Base URL | 字符串（URL） |
| `LLM_EMBEDDING_MODEL` | 模型配置 → Embedding | 模型名 | 字符串 |
| `LLM_EMBEDDING_API_KEY` | 模型配置 → Embedding | API Key | 秘密（Fernet 密文入库） |
| `HITL_NOTIFY_AUTO_APPROVE` | 运行参数 | notify 自动批准 | 布尔开关 |
| `MONITOR_PROBE_TIMEOUT_SECONDS` | 运行参数 | 探测超时（秒） | 浮点数 (0, 30] |
| `MONITOR_SWEEP_INTERVAL_SECONDS` | 运行参数 | 巡检间隔（秒） | 浮点数 [5, 3600] |
| `CMDB_DIFF_INTERVAL_SECONDS` | 运行参数 | CMDB 差异巡检（秒） | 浮点数 [60, 86400] |

**API Key 在 UI 中的三种语义：**

- **留空**：不修改当前密钥（保留数据库密文或环境变量回退）。
- **填写新值**：替换为新的 API Key（写入 Fernet 密文）。
- **勾选「清空密钥」**：将数据库行 `value` 设为 `NULL`，显式覆盖 `.env` 回退，运行时视为未配置。

GET 响应仅返回 `*_api_key_configured`（布尔）与 `*_api_key_source`（`database` / `environment` / `unset`），不回显明文或密文。

---

## 2. 配置来源优先级

```
数据库存在该 key 的行
    ├─ value 有值：使用数据库值（API Key 先解密）
    └─ value 为 NULL：仅适用于两个 API Key，表示管理员明确清空，不再回退 .env
数据库不存在该 key 的行
    └─ 使用 Settings / .env 兼容值
```

非秘密字段（URL、模型名、数值、布尔）在数据库行不存在或 `value` 为 `NULL` 时均回退到环境变量。

两个 API Key 是唯一例外：当数据库行存在且 `value` 为 `NULL` 时，表示「显式清空」，**不再**读取 `LLM_CHAT_API_KEY` / `LLM_EMBEDDING_API_KEY` 环境变量。

---

## 3. `CMDB_CREDENTIAL_KEY` 的生成、备份与共享影响面

`CMDB_CREDENTIAL_KEY` 是数据库可逆秘密值的**唯一** Fernet 根密钥，同时保护：

- `cmdb_assets.credential_password_encrypted`（CMDB 设备静态密码）
- `system_configs` 中 `LLM_CHAT_API_KEY` 与 `LLM_EMBEDDING_API_KEY` 的密文

**生成（在 `backend/` 目录下）：**

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

将输出写入 `backend/.env` 的 `CMDB_CREDENTIAL_KEY=`（勿提交到 Git）。

**备份要求：**

- 必须与数据库备份同等对待；丢失后上述两类密文均无法解密。
- 不得与 JWT `SECRET_KEY` 混用或互换。

**共享影响面：**

| 事件 | CMDB 密码 | LLM API Key |
| --- | --- | --- |
| 密钥泄露 | 可被解密 | 可被解密 |
| 密钥丢失/随意替换 | 现有密文不可解 | 现有密文不可解 |
| 仅改 `.env` 后重启 | 旧密文失效 | 旧密文失效 |

项目**未**引入第二个系统配置加密环境变量；所有数据库秘密均依赖此单一密钥。

---

## 4. 安全轮换步骤

本功能**不提供**在线轮换按钮。轮换 `CMDB_CREDENTIAL_KEY` 必须在计划维护窗口内完成，且**禁止**仅修改 `.env` 后重启。

**推荐流程：**

1. **读取旧密钥**：从安全备份或密钥管理系统取得当前 `CMDB_CREDENTIAL_KEY`。
2. **生成新密钥**：使用上文 Fernet 生成命令得到新密钥，暂存于安全位置。
3. **在同一维护事务/窗口内**：
   - 用旧密钥解密 `cmdb_assets.credential_password_encrypted` 全部行，用新密钥重新加密写回。
   - 用旧密钥解密 `system_configs` 中两个 API Key 行，用新密钥重新加密写回。
   - 将新密钥写入 `CMDB_CREDENTIAL_KEY` 并重启应用。
4. **验证**：抽查 CMDB 凭据解密、系统配置 GET（`configured` / `source` 正常）、一次 Agent Chat 与知识库语义检索（使用 Mock 或测试环境，避免生产费用）。

若旧密钥已丢失，只能重新录入 CMDB 密码与 LLM API Key，无法恢复历史密文。

---

## 5. `init_db.py` 种子范围

`init_db.py` **不**创建 8 个 LLM/Embedding 配置行。管理员须在 UI「模型配置」卡片中首次保存，或由运维通过 API 写入。

`init_db.py` **仅**幂等创建：

- 权限 `system_config:manage`（「管理系统配置」）
- 4 个运行参数种子（若对应键在库中不存在）：
  - `HITL_NOTIFY_AUTO_APPROVE`
  - `MONITOR_PROBE_TIMEOUT_SECONDS`
  - `MONITOR_SWEEP_INTERVAL_SECONDS`
  - `CMDB_DIFF_INTERVAL_SECONDS`

种子初始值取自运行 `init_db.py` 时进程解析到的 `Settings`（即当前 `.env`）。**已存在的行不会被覆盖**，因此 UI 修改后的值在重复执行 `init_db.py` 时保持不变。第二次运行应报告「运行配置种子：已齐全，跳过写入」。

---

## 6. API 路径与权限

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/system-config` | 读取脱敏后的有效配置 |
| `PUT` | `/api/v1/system-config/llm` | 更新 8 项 LLM/Embedding 参数 |
| `PUT` | `/api/v1/system-config/operations` | 更新 4 项 HITL/监控参数 |

**权限规则：**

- 三条接口均要求 `system_config:manage`（`require_permission`）。
- **超级管理员**（`is_superuser=true`）自动放行，无需单独分配权限。
- 普通用户无此权限时返回 **403**。
- 通过角色管理为运维账号授予 `system_config:manage` 后即可访问与保存。

**前端三层一致：**

- 侧边栏「系统管理 → 系统配置」入口（`permission: system_config:manage`）
- 路由 `/system-config`（`ProtectedRoute`）
- 上述三条后端 API

---

## 7. 四类运行时生效时机

进程内**不做**配置缓存；每次消费点从数据库读取最新有效值。修改后**无需重启**进程，但也不强行中断正在执行的请求或 sleep。

| 配置类别 | 消费点 | 生效时机 |
| --- | --- | --- |
| LLM Chat | 根 Agent、子 Agent、`chat_turn` 流式对话 | **下一次** Chat 模型调用 |
| LLM Embedding | 知识库文档入库、语义检索工具 | **下一次** Embedding 调用 |
| HITL 运行参数 | `notify` 提案自动批准逻辑 | **下一次** HITL 提案创建时 |
| 监控运行参数 | TCP 探活超时、monitor sweep 周期、CMDB diff 周期 | 探测超时：**下一轮** sweep；两个间隔：当前 sleep **结束后**下一轮重新读取 |

---

## 8. 升级步骤

在确认数据库备份后，按顺序执行：

1. **迁移表结构**（`backend/`）：
   ```bash
   uv run alembic upgrade head
   ```
   目标 revision：`a2f6c8d91e37`（创建 `system_configs` 表，不插入业务数据）。

2. **运行种子**：
   ```bash
   uv run python init_db.py
   ```
   预期：新增 1 条权限 `system_config:manage`；首次运行最多新增 4 条运行配置行；再次运行配置行增量为 0。

3. **角色授权**：在 UI「角色管理」中为运维角色勾选「管理系统配置」（`system_config:manage`），或使用超级管理员账号。

4. **配置模型**：进入「系统管理 → 系统配置」，在「模型配置」卡片填写 Chat/Embedding 端点与 API Key 并保存（8 个 LLM 键不会由种子自动创建）。

5. **确认审计**：在「审计日志」中应能看到 `bootstrap_system_configs`、`update_llm_system_config`、`update_operations_system_config` 等动作；详情中**不得**出现 API Key 明文或 Fernet 密文。

**验收（不产生真实模型调用）：**

```bash
# 后端
cd backend && uv run pytest -q tests/test_system_config_*.py

# 前端
cd frontend && pnpm test src/components/system-config/
```

---

## 9. 回滚提示

降级迁移 `a2f6c8d91e37` 会 **删除** `system_configs` 表及全部配置行。Alembic downgrade 需显式传递 `-x allow-destructive=true`。

**回滚前必须：**

1. 从 UI 或 `GET /api/v1/system-config` **导出非秘密值**（URL、模型名、数值、布尔）；API Key **不应**以明文导出（接口本身不回显）。
2. 记录当前 `.env` 中仍有效的 LLM/监控回退值。
3. 确认 CMDB 设备密码仍可通过当前 `CMDB_CREDENTIAL_KEY` 解密（回滚删除配置表**不会**删除 CMDB 密文，但密钥轮换失误仍会导致 CMDB 不可解）。

回滚后服务将回退到纯 `.env` / `Settings` 驱动；之前在数据库中的覆盖值将丢失，需凭导出记录或重新在 UI 配置。

---

## 相关文件

| 路径 | 说明 |
| --- | --- |
| `backend/app/services/system_config.py` | 白名单、优先级、有效值解析 |
| `backend/app/core/data_encryption.py` | Fernet 加解密（共享 `CMDB_CREDENTIAL_KEY`） |
| `backend/app/api/v1/system_config.py` | 三条 REST API |
| `backend/init_db.py` | 权限与 4 项运行参数种子 |
| `backend/alembic/versions/2026_08_13_1400-a2f6c8d91e37_system_configs.py` | 建表迁移 |
| `frontend/src/pages/SystemConfigPage.tsx` | 配置页面 |
