"""应用配置。

配置从稳定的 ``backend/.env`` 路径和环境变量加载，并在应用导入时完成校验。
生产环境对密钥、Cookie 和调试选项采用 fail-fast 策略。
"""

import os
from functools import lru_cache
from ipaddress import ip_network
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal, Self
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND_ROOT / ".env"
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://fastapi_admin_app:password@localhost:5432/fastapi_admin"
)


def _settings_env_file() -> Path | None:
    """测试环境不读取开发者本地 .env，避免已下线键或脏配置污染用例。"""
    if os.getenv("ENVIRONMENT") == "test":
        return None
    return ENV_FILE


type Environment = Literal["development", "test", "production"]


class Settings(BaseSettings):
    """类型安全的应用配置。"""

    model_config = SettingsConfigDict(
        env_file=_settings_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="forbid",
    )

    # 数据库
    DATABASE_URL: str = DEFAULT_DATABASE_URL
    MIGRATION_DATABASE_URL: SecretStr | None = None
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=100)
    DB_MAX_OVERFLOW: int = Field(default=5, ge=0, le=100)

    # LLM —— chat 和 embedding 各自独立配置，可以指向不同厂商/服务
    LLM_CHAT_BASE_URL: str = "http://127.0.0.1:8080/v1"
    LLM_CHAT_API_KEY: SecretStr | None = None
    LLM_CHAT_MODEL: str = "local-chat"
    LLM_CHAT_INPUT_COST_PER_MILLION_USD: float = Field(
        default=0.0, ge=0, allow_inf_nan=False
    )
    LLM_CHAT_OUTPUT_COST_PER_MILLION_USD: float = Field(
        default=0.0, ge=0, allow_inf_nan=False
    )
    LLM_EMBEDDING_BASE_URL: str = "http://127.0.0.1:8080/v1"
    LLM_EMBEDDING_API_KEY: SecretStr | None = None
    LLM_EMBEDDING_MODEL: str = "Qwen3-Embedding-0.6B"

    # 运维监控：TCP 探活 + CMDB 差异巡检
    MONITOR_PROBE_TIMEOUT_SECONDS: float = Field(default=3.0, gt=0, le=30)
    MONITOR_SWEEP_INTERVAL_SECONDS: float = Field(default=30.0, ge=5, le=3600)
    CMDB_DIFF_INTERVAL_SECONDS: float = Field(default=3600.0, ge=60, le=86_400)
    MONITOR_EVENT_RETENTION_DAYS: int = Field(default=7, ge=1, le=90)
    # 单轮探活的并发上限。探活是纯 I/O，串行执行时一轮耗时 = 目标数 × 超时，
    # 大批设备同时离线（最需要告警的时刻）会把扫描周期拖垮。太大则可能触发
    # 网络设备或防火墙的连接速率限制。
    MONITOR_PROBE_CONCURRENCY: int = Field(default=50, ge=1, le=500)
    # 过期探测记录的最小清理间隔。清理要对全表做窗口排序，属于低频维护动作，
    # 没必要每轮扫描（默认 30 秒）都跑一次。
    MONITOR_PURGE_MIN_INTERVAL_SECONDS: float = Field(default=3600.0, ge=0, le=86_400)
    # Netmiko 的两个超时量纲不同，分开配：
    # - CONN：建立 TCP 连接、认证、读 banner 的上限（Netmiko 默认 10）
    # - READ：单条命令等待提示符出现的上限（Netmiko 默认 10）；show running-config
    #   这类大输出靠它兜底，所以放宽到 60
    DEVICE_COMMAND_CONN_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0, le=120)
    DEVICE_COMMAND_READ_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0, le=600)
    # 设备命令专用线程池容量，决定「同时能有多少台设备在跑命令」。
    # 必须与 asyncio 默认线程池隔离：默认池同时承载密码哈希（core/security.py），
    # 单条设备命令最长占用 CONN+READ 秒，占满默认池会让登录接口直接 503，
    # 而且从密码限流指标上完全看不出原因。
    DEVICE_COMMAND_MAX_CONCURRENCY: int = Field(default=8, ge=1, le=64)
    # SSH 主机密钥校验。关闭时 Netmiko/Paramiko 用 AutoAddPolicy 接受任意主机密钥，
    # 而设备连接会发送特权账号明文口令，等于允许中间人直接窃取设备管理员密码。
    # 默认 False 是为了不破坏现网（开启后未登记指纹的设备会立刻连不上）；
    # 纳管流程补齐 known_hosts 后应尽快置 True，生产环境见下方 fail-fast 校验。
    DEVICE_SSH_STRICT_HOST_KEY: bool = False
    # 额外的 known_hosts 文件路径；为空时只用系统默认 (~/.ssh/known_hosts)。
    DEVICE_SSH_KNOWN_HOSTS_FILE: str | None = None

    # 子 Agent Spawn 配额与回执回收
    AGENT_MAX_CONCURRENT_CHILDREN: int = Field(default=5, ge=1)
    AGENT_MAX_SPAWN_DEPTH: int = Field(default=2, ge=1)
    AGENT_MAX_CHILDREN_PER_SESSION: int = Field(default=50, ge=1)
    AGENT_MAX_TOTAL_CHILD_COST_USD: float = Field(
        default=5.0, ge=0, allow_inf_nan=False
    )
    AGENT_CHILD_MAX_STEPS: int = Field(default=20, ge=1)
    AGENT_CHILD_MAX_COST_USD: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    AGENT_CHILD_MAX_WALL_TIME_SECONDS: float = Field(
        default=120.0, gt=0, allow_inf_nan=False
    )
    AGENT_CLOSE_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    # turn 租约超时。超过这个时长的租约会被下一次请求接管，避免进程存活但 turn
    # 任务已经消失时会话被永久锁死（进程重启有 recover_active_turns 兜底，
    # 但进程不重启就没人清）。
    #
    # ⚠️ 这个值必须**大于**单轮最坏耗时，否则会抢占一个还在正常执行的 turn，
    # 造成两个 turn 并发写同一份 transcript——那比卡住更糟。
    # 最坏耗时 ≈ AGENT_CHILD_MAX_STEPS × (LLM 超时 + DEVICE_COMMAND_CONN + READ)
    #          = 20 × (60 + 15 + 60) = 2700 秒。默认 3600 留出余量。
    AGENT_TURN_LEASE_TIMEOUT_SECONDS: float = Field(default=3600.0, gt=0, le=86_400)
    # refresh 会话历史清理的轮询间隔（后台循环，见 services/session_cleanup.py）
    SESSION_CLEANUP_INTERVAL_SECONDS: float = Field(default=3600.0, ge=60, le=86_400)
    AGENT_TERMINAL_RECEIPT_TTL_SECONDS: float = Field(
        default=300.0, ge=0, allow_inf_nan=False
    )
    AGENT_RECEIPT_GC_INTERVAL_SECONDS: float = Field(
        default=60.0, gt=0, allow_inf_nan=False
    )
    # 数据库可逆秘密值的共享 Fernet 密钥：同时保护 CMDB 静态密码和 LLM API Key。
    # 泄露、丢失或轮换会同时影响两类密文；必须稳定备份，禁止与 JWT SECRET_KEY 混用。
    CMDB_CREDENTIAL_KEY: SecretStr | None = None

    # JWT / 会话
    SECRET_KEY: SecretStr | None = None
    ALGORITHM: Literal["HS256"] = "HS256"
    JWT_ISSUER: str = "fastapi-admin"
    JWT_AUDIENCE: str = "fastapi-admin-api"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=30, ge=1, le=30)
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=7, ge=1, le=30)
    REFRESH_SESSION_REPLAY_GRACE_DAYS: int = Field(default=1, ge=0, le=7)
    REFRESH_SESSION_HISTORY_RETENTION_DAYS: int = Field(default=30, ge=7, le=365)
    REFRESH_SESSION_CLEANUP_BATCH_SIZE: int = Field(default=1000, ge=100, le=10_000)

    # 登录保护（进程内兜底；生产仍应在网关配置共享限流）
    LOGIN_RATE_LIMIT_ATTEMPTS: int = Field(default=5, ge=1, le=100)
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, ge=1, le=3600)
    REGISTRATION_RATE_LIMIT_ATTEMPTS: int = Field(default=5, ge=1, le=100)
    PASSWORD_HASH_MAX_CONCURRENCY: int = Field(default=4, ge=1, le=32)
    PASSWORD_HASH_QUEUE_TIMEOUT_SECONDS: int = Field(default=5, ge=1, le=30)

    # CORS / Cookie / 代理
    BACKEND_CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"
    ALLOWED_HOSTS: str = "localhost,127.0.0.1,test"
    COOKIE_SECURE: bool = False
    TRUSTED_PROXY_CIDRS: str = "127.0.0.1/32,::1/128"

    # 应用
    ENVIRONMENT: Environment = "development"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8000, ge=1, le=65535)
    LOG_LEVEL: Literal["critical", "error", "warning", "info", "debug"] = "info"
    # 仅在排查 SQL 时开启；默认关闭，避免控制台被引擎日志淹没
    SQL_ECHO: bool = False
    API_V1_PREFIX: str = "/api/v1"
    REGISTRATION_ENABLED: bool = False

    # 初始化超级管理员；仅 init_db.py 使用，不提供可工作的默认密码
    INIT_SUPERUSER_USERNAME: str | None = None
    INIT_SUPERUSER_EMAIL: str | None = None
    INIT_SUPERUSER_PASSWORD: SecretStr | None = None

    @field_validator("TRUSTED_PROXY_CIDRS")
    @classmethod
    def validate_trusted_proxy_cidrs(cls, value: str) -> str:
        """在启动时验证可信代理网段。"""
        for item in value.split(","):
            candidate = item.strip()
            if candidate:
                ip_network(candidate, strict=False)
        return value

    @field_validator("MIGRATION_DATABASE_URL")
    @classmethod
    def validate_migration_database_url(
        cls,
        value: SecretStr | None,
    ) -> SecretStr | None:
        """Reject unusable migration URLs without exposing their credentials."""
        if value is None:
            return None
        try:
            database_url = make_url(value.get_secret_value())
        except ArgumentError as exc:
            raise ValueError("MIGRATION_DATABASE_URL 格式无效") from exc
        if database_url.get_backend_name() != "postgresql":
            raise ValueError("数据库迁移仅支持 PostgreSQL")
        return value

    @field_validator("CMDB_CREDENTIAL_KEY")
    @classmethod
    def validate_cmdb_credential_key(cls, value: SecretStr | None) -> SecretStr | None:
        """在启动时校验密钥格式，避免录入了一个格式错误的值等到真正使用才报错。"""
        if value is None:
            return None
        if not value.get_secret_value().strip():
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

    @model_validator(mode="after")
    def validate_security_settings(self) -> Self:
        """校验跨字段安全约束，并为本地开发生成临时密钥。"""
        if self.SECRET_KEY is None:
            if self.ENVIRONMENT == "production":
                raise ValueError("生产环境必须显式配置 SECRET_KEY")
            self.SECRET_KEY = SecretStr(token_urlsafe(48))

        if len(self.SECRET_KEY.get_secret_value()) < 32:
            raise ValueError("SECRET_KEY 至少需要 32 个字符")

        if self.ENVIRONMENT == "production":
            try:
                database_url = make_url(self.DATABASE_URL)
            except ArgumentError as exc:
                raise ValueError("DATABASE_URL 格式无效") from exc
            password = database_url.password or ""
            if database_url.get_backend_name() != "postgresql":
                raise ValueError("生产环境仅支持 PostgreSQL")
            if password.casefold() in {"", "change-me", "changeme", "password", "postgres"}:
                raise ValueError("生产环境必须配置非占位数据库密码")
            if self.MIGRATION_DATABASE_URL is not None:
                migration_url = make_url(self.MIGRATION_DATABASE_URL.get_secret_value())
                migration_password = migration_url.password or ""
                if migration_password.casefold() in {
                    "",
                    "change-me",
                    "changeme",
                    "password",
                    "postgres",
                }:
                    raise ValueError("生产迁移账号必须配置非占位数据库密码")
            if self.DEBUG:
                raise ValueError("生产环境禁止启用 DEBUG")
            if not self.COOKIE_SECURE:
                raise ValueError("生产环境必须启用 COOKIE_SECURE")
            if "*" in self.cors_origins_list:
                raise ValueError("携带凭据的生产 CORS 配置禁止使用通配符")
            if any(
                urlsplit(origin).scheme != "https"
                or urlsplit(origin).hostname in {None, "localhost", "127.0.0.1", "::1"}
                for origin in self.cors_origins_list
            ):
                raise ValueError("生产环境 CORS 来源必须是显式的 HTTPS 公网域名")
            if not self.allowed_hosts_list or any("*" in host for host in self.allowed_hosts_list):
                raise ValueError("生产环境必须显式配置 ALLOWED_HOSTS，且禁止通配符")
            if any(
                host.casefold() in {"localhost", "127.0.0.1", "::1", "test"}
                for host in self.allowed_hosts_list
            ):
                raise ValueError("生产环境 ALLOWED_HOSTS 不能使用开发默认主机")
            if self.INIT_SUPERUSER_PASSWORD is not None:
                password = self.INIT_SUPERUSER_PASSWORD.get_secret_value()
                if len(password) < 12:
                    raise ValueError("生产环境初始超级管理员密码至少需要 12 个字符")

        return self

    @property
    def secret_key(self) -> str:
        """返回仅供签名组件使用的密钥明文。"""
        if self.SECRET_KEY is None:  # pragma: no cover - 已由模型校验保证
            raise RuntimeError("SECRET_KEY 未初始化")
        return self.SECRET_KEY.get_secret_value()

    @property
    def llm_chat_api_key(self) -> str:
        """Return the chat model's API key, or an empty string when none is configured."""
        if self.LLM_CHAT_API_KEY is None:
            return ""
        return self.LLM_CHAT_API_KEY.get_secret_value()

    @property
    def llm_embedding_api_key(self) -> str:
        """Return the embedding model's API key, or an empty string when none is configured."""
        if self.LLM_EMBEDDING_API_KEY is None:
            return ""
        return self.LLM_EMBEDDING_API_KEY.get_secret_value()

    @property
    def migration_database_url(self) -> str:
        """Return the privileged URL only to the one-off migration process.

        Development may reuse the application connection for convenience. A
        production Alembic run must inject a distinct migration role so the
        long-running web process can keep a DML-only credential.
        """
        if self.MIGRATION_DATABASE_URL is None:
            if self.ENVIRONMENT == "production":
                raise RuntimeError("生产数据库迁移必须显式配置 MIGRATION_DATABASE_URL")
            return self.DATABASE_URL

        migration_url = self.MIGRATION_DATABASE_URL.get_secret_value()
        if self.ENVIRONMENT == "production":
            runtime_username = make_url(self.DATABASE_URL).username
            migration_username = make_url(migration_url).username
            if migration_username == runtime_username:
                raise RuntimeError("生产迁移账号必须与运行时数据库账号分离")
        return migration_url

    @property
    def cors_origins_list(self) -> list[str]:
        """将逗号分隔的 CORS 来源转换为列表。"""
        return [origin.strip() for origin in self.BACKEND_CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def allowed_hosts_list(self) -> list[str]:
        """Return the validated host-header allowlist."""
        return [host.strip() for host in self.ALLOWED_HOSTS.split(",") if host.strip()]


@lru_cache
def get_settings() -> Settings:
    """获取进程内配置单例。"""
    return Settings()


settings = get_settings()
