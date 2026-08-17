"""FastAPI application factory and cross-cutting HTTP policies."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.agent.executors import shutdown_device_executor
from app.agent.hitl_execution import reconcile_executing_proposals
from app.agent.spawn import run_receipt_gc_loop, spawn_manager
from app.agent.ws_hub import WsSpawnEventPublisher, hub
from app.api.router import api_router
from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.llm import close_llm_clients
from app.core.security import PasswordHashOverloadedError
from app.crud.agent_session import agent_session_crud
from app.crud.base import RelatedObjectsNotFoundError
from app.crud.role import RoleInUseError
from app.crud.user import LastActiveSuperuserError
from app.services.cmdb_diff import run_cmdb_diff_loop
from app.services.monitor_sweep import run_monitor_sweep_loop
from app.services.session_cleanup import run_session_cleanup_loop


def configure_logging() -> None:
    """
    配置应用日志。

    根日志遵循 ``LOG_LEVEL``；SQLAlchemy 引擎/连接池默认仅输出 WARNING 及以上，
    需要 SQL 排障时设置 ``SQL_ECHO=true``。
    """
    logging.basicConfig(
        level=logging.getLevelName(settings.LOG_LEVEL.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    sql_level = logging.INFO if settings.SQL_ECHO else logging.WARNING
    logging.getLogger("sqlalchemy.engine").setLevel(sql_level)
    logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.dialects").setLevel(logging.WARNING)


configure_logging()
logger = logging.getLogger(__name__)


def _error_content(
    status_code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    """Build the public error envelope used by every exception handler."""
    return {"code": status_code, "data": data, "message": message}


def validate_single_worker_environment(environment: Mapping[str, str]) -> None:
    """拒绝已知的多 worker 环境配置，确保 Agent Spawn 只在单进程内运行。"""
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw = environment.get(name)
        if raw is None:
            continue
        try:
            workers = int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{name} 必须是整数 1") from exc
        if workers != 1:
            raise RuntimeError(
                "当前 Agent Spawn 运行时只支持 1 个 Uvicorn worker；"
                f"检测到 {name}={raw}"
            )


def _warn_on_background_task_death(task: asyncio.Task[None]) -> None:
    """常驻后台循环不应正常返回；无论异常退出还是静默返回都要留下证据。

    没有这个回调时，裸 create_task 起的循环一旦死掉就完全无声——监控/巡检
    停止工作，日志里却只有当初那一条异常，运维不会意识到功能已经失效。
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.critical("后台任务 %s 异常退出，相关功能已停止", task.get_name(), exc_info=exc)
    else:
        logger.critical("后台任务 %s 意外返回，相关功能已停止", task.get_name())


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Reconcile crashed child-agent state, run background jobs, then release resources.

    Startup order: close every orphaned child-agent row this fresh process
    doesn't own before starting the monitor sweep, CMDB diff, and receipt GC
    loops. Shutdown order: cancel and await all three loops, close every
    child this process still owns, then dispose the database engine last.
    """
    validate_single_worker_environment(os.environ)
    if not settings.DEVICE_SSH_STRICT_HOST_KEY:
        # 不做成 fail-fast：开启会让所有未登记指纹的设备立刻连不上，属于破坏性
        # 变更，必须先用 ssh-keyscan 批量补齐 known_hosts。这里只保证「没开」这件事
        # 每次启动都被看见，而不是无声地一直关着。
        logger.warning(
            "设备 SSH 主机密钥校验未开启（DEVICE_SSH_STRICT_HOST_KEY=false）："
            "设备连接会发送特权账号明文口令，中间人可直接窃取。"
            "补齐 known_hosts 后请尽快置为 true"
        )
    await reconcile_executing_proposals(AsyncSessionLocal)
    async with AsyncSessionLocal() as recover_db:
        await agent_session_crud.recover_active_turns(recover_db)
        await recover_db.commit()
    await spawn_manager.reconcile_startup()
    spawn_manager.set_event_publisher(WsSpawnEventPublisher(hub))
    monitor_task = asyncio.create_task(run_monitor_sweep_loop(), name="monitor_sweep")
    diff_task = asyncio.create_task(run_cmdb_diff_loop(), name="cmdb_diff")
    gc_task = asyncio.create_task(run_receipt_gc_loop(spawn_manager), name="receipt_gc")
    cleanup_task = asyncio.create_task(run_session_cleanup_loop(), name="session_cleanup")
    background_tasks = (monitor_task, diff_task, gc_task, cleanup_task)
    for task in background_tasks:
        task.add_done_callback(_warn_on_background_task_death)
    yield
    for task in background_tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await spawn_manager.shutdown()
    # 设备线程池不等待在跑的命令（Netmiko 线程不可取消，可能还要几十秒）。
    shutdown_device_executor()
    await close_llm_clients()
    await engine.dispose()


def create_app() -> FastAPI:
    """Create a configured FastAPI application."""
    production = settings.ENVIRONMENT == "production"
    app = FastAPI(
        title="FastAPI 权限管理系统",
        description="基于 RBAC 模型的权限管理系统 API",
        version="1.0.0",
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["DELETE", "GET", "OPTIONS", "PATCH", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list)
    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted_hosts=settings.TRUSTED_PROXY_CIDRS,
    )
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @app.get("/health", tags=["系统"])
    async def health_check() -> dict[str, str]:
        """Return a process-level liveness signal without blocking a worker thread."""
        return {"status": "ok"}

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        """Preserve the HTTP status while applying the shared error envelope."""
        detail = exc.detail
        message = detail if isinstance(detail, str) else "请求失败"
        data = None if isinstance(detail, str) else jsonable_encoder(detail)
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content=_error_content(exc.status_code, message, data),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Return machine-readable validation details with HTTP 422."""
        errors = [
            {key: value for key, value in error.items() if key != "input"} for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            headers={"Cache-Control": "no-store"},
            content=_error_content(
                422,
                "请求参数校验失败",
                {"errors": jsonable_encoder(errors)},
            ),
        )

    @app.exception_handler(IntegrityError)
    async def integrity_exception_handler(
        request: Request,
        exc: IntegrityError,
    ) -> JSONResponse:
        """Convert database constraint races into a safe conflict response."""
        logger.info("数据库约束冲突，路径: %s", request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=409,
            content=_error_content(409, "资源已存在或仍被其他数据引用"),
        )

    @app.exception_handler(PasswordHashOverloadedError)
    async def password_hash_overloaded_handler(
        _: Request,
        exc: PasswordHashOverloadedError,
    ) -> JSONResponse:
        """Fail fast when bounded password-worker capacity is exhausted."""
        logger.warning("密码哈希工作池过载", exc_info=exc)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "1"},
            content=_error_content(503, "认证服务繁忙，请稍后重试"),
        )

    @app.exception_handler(RelatedObjectsNotFoundError)
    async def missing_relation_exception_handler(
        _: Request,
        exc: RelatedObjectsNotFoundError,
    ) -> JSONResponse:
        """Reject relation replacement unless every requested ID exists."""
        relation_name = {"role": "角色", "permission": "权限"}.get(
            exc.relation,
            exc.relation,
        )
        return JSONResponse(
            status_code=422,
            content=_error_content(
                422,
                f"以下{relation_name}不存在或已删除",
                {"relation": exc.relation, "missing_ids": list(exc.missing_ids)},
            ),
        )

    @app.exception_handler(LastActiveSuperuserError)
    async def last_superuser_exception_handler(
        _: Request,
        exc: LastActiveSuperuserError,
    ) -> JSONResponse:
        """Prevent an administrative lockout."""
        return JSONResponse(status_code=409, content=_error_content(409, str(exc)))

    @app.exception_handler(RoleInUseError)
    async def role_in_use_exception_handler(
        _: Request,
        exc: RoleInUseError,
    ) -> JSONResponse:
        """Report a concurrent-safe role deletion conflict."""
        return JSONResponse(
            status_code=409,
            content=_error_content(
                409,
                str(exc),
                {"user_count": exc.user_count},
            ),
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Hide implementation details from unexpected server errors."""
        logger.error("未捕获异常，路径: %s", request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=_error_content(500, "服务器内部错误"),
        )

    return app


app = create_app()
