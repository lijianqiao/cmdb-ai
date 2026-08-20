# 部署指南

> **项目**: fastapi-admin 权限管理系统  
> **日期**: 2025-08-07

---

## 1. 环境要求

| 组件 | 版本要求 |
|------|----------|
| Python | 3.14.3 |
| Node.js | >= 18 |
| PostgreSQL | >= 14 |
| Nginx | >= 1.20 |
| uv | latest |

---

## 2. 后端部署

### 2.1 安装依赖

```bash
cd backend
uv sync
```

### 2.2 配置环境变量

复制 `.env.example` 为 `.env` 并修改配置：

```bash
cp .env.example .env
```

关键配置项：

```ini
# Web 运行时连接：只授予业务表 DML 权限
DATABASE_URL=postgresql+psycopg://fastapi_admin_app:your_password@localhost:5432/fastapi_admin

# JWT 密钥（至少 32 字符随机字符串）
SECRET_KEY=your-random-secret-key-at-least-32-characters

# CORS 白名单（生产环境域名）
BACKEND_CORS_ORIGINS=https://your-domain.com
ALLOWED_HOSTS=your-domain.com

# 仅填写实际反向代理地址/网段
TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128

# Cookie 安全（生产环境设为 true）
COOKIE_SECURE=true

# 生产安全校验
ENVIRONMENT=production

# 关闭调试模式
DEBUG=false
```

### 2.3 数据库初始化

**由 DBA 创建数据库、独立角色并预装扩展：**

```sql
CREATE ROLE fastapi_admin_app LOGIN PASSWORD 'replace-from-secret-manager';
CREATE ROLE fastapi_admin_migrator LOGIN PASSWORD 'replace-from-secret-manager';
CREATE DATABASE fastapi_admin;
GRANT CONNECT ON DATABASE fastapi_admin TO fastapi_admin_app, fastapi_admin_migrator;

\connect fastapi_admin

-- pg_trgm 由 DBA 预装；迁移账号不需要数据库级 CREATE 权限。
CREATE EXTENSION IF NOT EXISTS pg_trgm;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO fastapi_admin_migrator;
GRANT USAGE ON SCHEMA public TO fastapi_admin_app;
```

以 `fastapi_admin_migrator` 连接并在首次迁移前设置默认权限：

```sql
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fastapi_admin_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO fastapi_admin_app;
```

**运行 Alembic 迁移：** 通过部署平台的临时 Secret 仅向本次迁移进程注入
`MIGRATION_DATABASE_URL`，不得写入 Web 容器的长期环境变量。

```bash
cd backend
# MIGRATION_DATABASE_URL 由 CI/CD Secret 注入，使用 fastapi_admin_migrator 角色
uv run alembic upgrade head
```

迁移完成后，以 `fastapi_admin_migrator` 执行一次现有对象授权；默认权限会覆盖后续迁移创建的对象：

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    users, roles, permissions, user_roles, role_permissions,
    refresh_session_families, refresh_sessions
    TO fastapi_admin_app;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_logs FROM fastapi_admin_app;
GRANT SELECT, INSERT ON TABLE audit_logs TO fastapi_admin_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fastapi_admin_app;
REVOKE ALL ON TABLE alembic_version FROM fastapi_admin_app;
```

默认权限会先授予新表通用 DML；每次新增审计/追加型表时，都必须在同一迁移发布步骤中
显式回收 `UPDATE`、`DELETE`、`TRUNCATE`，再只保留所需的 `SELECT`/`INSERT`。

Alembic 迁移链以 PostgreSQL 为唯一目标；测试中的 SQLite 仅通过 ORM metadata
创建隔离库，不能用于验证生产迁移。查询索引使用 `CREATE INDEX CONCURRENTLY`。
用户名、邮箱和权限码归一化只更新不合规行，但会在 `users`/`permissions` 上获取一个
短暂 `EXCLUSIVE` 锁：普通查询继续可用，写入和 `SELECT FOR UPDATE` 会等待。请把该次
升级安排在短只读窗口，并先处理迁移报告的任何大小写重复值。

迁移固定使用 `public` schema，并拒绝 URL 中的 `search_path` 覆盖。完整迁移链验证
必须创建独立的临时数据库，不能依靠同库临时 schema 隔离。所有 downgrade 默认拒绝
执行；确认目标数据库和备份后，才可显式运行：

```bash
uv run alembic -x allow-destructive=true downgrade <revision>
```

**创建初始超级管理员：**

在一次性安全环境中设置 `INIT_SUPERUSER_USERNAME`、`INIT_SUPERUSER_EMAIL`、
`INIT_SUPERUSER_PASSWORD`，然后运行：

```bash
uv run python init_db.py
```

脚本不会建表、打印密码、覆盖账户或提升已有用户；已有任意超级管理员时会拒绝再次创建。

### 2.4 启动后端服务

**开发模式：**

```bash
cd backend
uv run python main.py
```

**生产模式（Linux/macOS，由外层进程管理器监督）：**

```bash
cd backend
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-proxy-headers
```

**Windows：**

```powershell
cd backend
uv run python main.py
```

Windows 必须通过 `main.py` 启动，以使用与异步 psycopg 兼容的 Selector 事件循环。

单实例保持一个异步 worker，使应用内账户/IP 限流全局一致。需要多进程或多实例时，必须先在共享 API 网关/Redis 层配置等价认证限流。

```text
当前进程内 SpawnManager 只支持单 Uvicorn worker 和单应用实例。
配置 WEB_CONCURRENCY>1 或 UVICORN_WORKERS>1 时应用拒绝启动。
多实例部署需要未来引入分布式任务所有权，本版本不支持。
```

应用启动时 `validate_single_worker_environment` 会检查 `WEB_CONCURRENCY` 与 `UVICORN_WORKERS` 环境变量；任一值大于 1 即抛出 `RuntimeError` 并拒绝启动。Gunicorn 部署时同样须保持 `--workers 1`。

#### 需要更多容量时怎么扩

**先别想 worker 数。** 加 worker 修不了会最先坏掉的东西，反而会让情况变糟：

| 资源 | 默认值 | 加 worker 之后 |
| :--- | :--- | :--- |
| 数据库连接池 | `DB_POOL_SIZE=5` + `DB_MAX_OVERFLOW=5` = 10 | **变差**：每进程一份池，N×10 个连接直接压向 PostgreSQL 的 `max_connections` |
| 设备命令线程池 | `DEVICE_COMMAND_MAX_CONCURRENCY=8` | 不变，本来就是进程内上限 |
| LLM 服务商限流 | 外部约束 | 不变 |
| 事件循环 | I/O 密集，Netmiko 等阻塞调用已丢进专用线程池 | **最不容易先满的一个** |

**扩容顺序**：先调 `DB_POOL_SIZE` / `DEVICE_COMMAND_MAX_CONCURRENCY`，观察真实瓶颈；
这两个都调到位仍不够，再考虑多实例。

**多实例的正确做法是会话亲和，不是拆微服务。** `ws_hub` 持有的是活的 WebSocket
对象、`turn_registry` 持有的是正在跑的 asyncio.Task——这两样不可序列化、不可跨进程
搬运。把 Agent 运行时抽成独立服务并不能解除这个约束，那个新服务自己仍然只能单实例。

真正要解决的问题是「**把一个 session 路由到持有它的那个进程**」：跑 N 个应用实例，
按 `session_id` 做一致性哈希路由（nginx `hash` 或一层小路由），每个实例持有互不相交
的一组会话，三个进程内 registry 全都保持语义不变。不需要消息总线，不需要序列化 socket。

代价是实例挂掉会丢正在跑的 turn——但这一点单实例下**今天就已经如此**，不是新增风险。

**定时清理 refresh 历史（建议每小时）：**

```bash
cd backend
uv run python cleanup_sessions.py
```

清理器使用短事务、分批行锁与 `SKIP LOCKED`，可安全并行执行；保留期和批量大小由 `REFRESH_SESSION_*` 环境变量配置。

**审计日志保留（DBA 职责，应用不参与）：**

`audit_logs` 是仅追加表：`fastapi_admin_app` 角色被显式回收了 `UPDATE`/`DELETE`/`TRUNCATE`，
因此应用侧的清理脚本**不能也不应该**删除审计数据。该表只增不减，且未认证的失败登录
（`login_failed`）也会写入，必须由 DBA 建立保留策略，否则分页查询会随时间退化：

- 归档/删除操作使用独立的高权限角色，**不要向 Web 运行时角色授予 `DELETE`**；
- 保留期按合规要求确定。等保 2.0 要求日志留存不少于 6 个月，推荐取 **1 年**。

**为什么应用不做这件事**：append-only 是一条刻意的安全控制。应用被攻破或代码
有 bug 时，都无法抹掉已经写下的审计痕迹——对一个能重启交换机的系统，这个取证
属性比省下的磁盘空间值钱得多。曾经有一版在应用里加了自动清理循环，
因为违反这条控制而被撤回。

**按量级选方案**（实测约 56 行/天，即 ~13 MB/年；团队放大 20 倍也才 ~260 MB/年）：

| 量级 | 做法 |
| :--- | :--- |
| **千万行以下**（绝大多数内部部署） | 定时 `DELETE`，见下方脚本。分区在这个量级没有收益，反而要把主键改成 `(id, created_at)` 复合键——那是一次要迁数据的破坏性变更 |
| 千万行以上 | 再考虑按 `created_at` 做月度 `PARTITION BY RANGE`，过期分区 `DETACH PARTITION` + 归档后 `DROP`。此时 `DROP PARTITION` 相对 `DELETE` 才有实质优势（不产生大量 WAL、不留膨胀） |

现成的定时清理（用独立的高权限角色执行，**不是** Web 运行时角色）：

```sql
-- 分批删除，避免长事务与 WAL 堆积；反复执行直到返回 0 行
DELETE FROM audit_logs
WHERE id IN (
    SELECT id FROM audit_logs
    WHERE created_at < now() - INTERVAL '365 days'
    ORDER BY created_at, id
    LIMIT 1000
);
```

用 `pg_cron` 每日调度（或宿主机 crontab 调 `psql`）：

```sql
SELECT cron.schedule('audit-retention', '0 3 * * *', $$
    DELETE FROM audit_logs WHERE id IN (
        SELECT id FROM audit_logs
        WHERE created_at < now() - INTERVAL '365 days'
        ORDER BY created_at, id LIMIT 10000
    );
$$);
```

按当前量级，这个任务在头一年里每次都会删 0 行——**现在就可以配上，不必等表长大**。

---

## 3. 前端部署

### 3.1 安装依赖

```bash
cd frontend
npm install
```

### 3.2 配置环境变量

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

```ini
VITE_API_BASE_URL=https://your-domain.com/api/v1
```

### 3.3 构建生产包

```bash
cd frontend
npm run build
```

构建产物在 `dist/` 目录。

---

## 4. Nginx 配置

```nginx
server {
    listen 80;
    server_name your-domain.com;

    # 前端静态文件
    root /path/to/frontend/dist;
    index index.html;

    # 前端路由（SPA 回退）
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API 反向代理
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 静态资源缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # Gzip 压缩
    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;
    gzip_min_length 1024;
}
```

### HTTPS 配置（推荐）

使用 Let's Encrypt 免费证书：

```bash
sudo certbot --nginx -d your-domain.com
```

---

## 5. 系统服务（Systemd）

### 后端服务

创建 `/etc/systemd/system/fastapi-admin.service`：

```ini
[Unit]
Description=FastAPI Admin Backend
After=network.target postgresql.service

[Service]
Type=exec
User=www-data
WorkingDirectory=/path/to/backend
EnvironmentFile=/path/to/backend/.env
ExecStart=/path/to/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable fastapi-admin
sudo systemctl start fastapi-admin
```

---

## 6. 验证部署

1. **后端健康检查：**
   ```bash
   curl https://your-domain.com/health
   ```

2. **Swagger 文档：**
   仅非生产环境提供 `/docs`；生产环境默认关闭 Swagger、ReDoc 和 OpenAPI JSON。

3. **前端页面：**
   访问 `https://your-domain.com`

4. **管理员登录：** 使用一次性初始化时显式设置的凭据；系统不存在默认密码。

---

## 7. 环境变量说明

### 后端环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `DATABASE_URL` | Web 运行时 PostgreSQL URL（DML-only 角色） | `postgresql+psycopg://fastapi_admin_app:password@localhost:5432/fastapi_admin` |
| `MIGRATION_DATABASE_URL` | 仅 Alembic 进程使用的独立 DDL 角色 URL；生产迁移必填 | 开发回退到 `DATABASE_URL` |
| `SECRET_KEY` | JWT 签名密钥（≥32 字符） | - |
| `ALGORITHM` | JWT 算法 | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Access Token 过期时间（分钟） | `30` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Refresh Token 过期时间（天） | `7` |
| `REFRESH_SESSION_REPLAY_GRACE_DAYS` | 过期 token 历史额外保留天数 | `1` |
| `REFRESH_SESSION_HISTORY_RETENTION_DAYS` | 过期会话族历史保留天数 | `30` |
| `REFRESH_SESSION_CLEANUP_BATCH_SIZE` | 清理任务单批最大行数 | `1000` |
| `DB_POOL_SIZE` | 每进程数据库连接池大小 | `5` |
| `DB_MAX_OVERFLOW` | 每进程连接池最大溢出 | `5` |
| `BACKEND_CORS_ORIGINS` | CORS 白名单（逗号分隔） | `http://localhost:5173,http://localhost:3000` |
| `COOKIE_SECURE` | Cookie Secure 标志 | `false` |
| `ALLOWED_HOSTS` | Host 头白名单（逗号分隔） | `localhost,127.0.0.1,test` |
| `TRUSTED_PROXY_CIDRS` | 可解释代理头的直连代理网段 | `127.0.0.1/32,::1/128` |
| `REGISTRATION_ENABLED` | 是否开启自助注册 | `false` |
| `PASSWORD_HASH_MAX_CONCURRENCY` | 每进程密码哈希最大并发 | `4` |
| `PASSWORD_HASH_QUEUE_TIMEOUT_SECONDS` | 密码哈希排队超时（秒） | `5` |
| `DEBUG` | 调试模式 | `false` |

### 前端环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `VITE_API_BASE_URL` | API 基础路径 | `http://localhost:8000/api/v1` |

---

## 8. 常见问题

### Q: Windows 上异步 psycopg 报 Proactor 不兼容？

A: 使用 `uv run python main.py` 启动；该入口为 Python 3.14 显式创建 Selector loop。Alembic 已使用同步 psycopg 连接，不受此限制。

### Q: psycopg 安装失败？

A: 使用 `psycopg[binary]` 包含预编译二进制。如果仍失败，尝试：
```bash
uv add "psycopg[binary]>=3.2" --no-build
```

生产部署建议：`psycopg[binary]` 自带静态编译的 libpq，版本可能落后于系统安全补丁，官方建议仅用于开发/测试。生产镜像若已具备构建工具链和系统 libpq，优先改用 `psycopg[c]`（对源码编译、链接系统 libpq），随系统一起获得 TLS/协议层的安全更新。

### Q: 数据库迁移失败？

A: 确保 PostgreSQL 已启动；Web 连接使用 `DATABASE_URL`，迁移进程另行注入
`MIGRATION_DATABASE_URL`。生产 Web 进程不得持有后者：
```bash
psql $DATABASE_URL -c "SELECT 1;"
```
