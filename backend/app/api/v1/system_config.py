"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: system_config.py
@DateTime: 2026-08-13 13:10
@Docs: 系统运行配置 API：RBAC 保护、审计与秘密脱敏。
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.data_encryption import DataDecryptError, DataEncryptionKeyMissingError
from app.core.database import get_db
from app.core.deps import get_client_ip, require_permission
from app.crud.system_config import system_config_crud
from app.models.user import User
from app.schemas.common import ResponseEnvelope, success_response
from app.schemas.system_config import (
    LlmSystemConfigUpdate,
    OperationsSystemConfigUpdate,
    SystemConfigResponse,
)
from app.services.system_config import (
    KEY_CMDB_DIFF_INTERVAL_SECONDS,
    KEY_HITL_NOTIFY_AUTO_APPROVE,
    KEY_LLM_CHAT_BASE_URL,
    KEY_LLM_CHAT_INPUT_COST_PER_MILLION_USD,
    KEY_LLM_CHAT_MODEL,
    KEY_LLM_CHAT_OUTPUT_COST_PER_MILLION_USD,
    KEY_LLM_EMBEDDING_BASE_URL,
    KEY_LLM_EMBEDDING_MODEL,
    KEY_MONITOR_PROBE_TIMEOUT_SECONDS,
    KEY_MONITOR_SWEEP_INTERVAL_SECONDS,
    KEY_MONITOR_EVENT_RETENTION_DAYS,
    LLM_CONFIG_KEYS,
    OPERATIONS_CONFIG_KEYS,
    build_system_config_response,
    save_llm_config,
    save_operations_config,
)
from app.utils.audit import log_audit

router = APIRouter()

_ENCRYPTION_KEY_MISSING_DETAIL = (
    "未配置 CMDB_CREDENTIAL_KEY，无法保存系统配置秘密值。"
    "该密钥同时用于 CMDB 凭据与系统配置 API Key 加密，请联系管理员配置。"
)
_DECRYPT_ERROR_DETAIL = (
    "数据库密文无法解密。CMDB_CREDENTIAL_KEY 同时保护 CMDB 凭据与系统配置 API Key，"
    "请恢复原始密钥而非随意更换。"
)


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


async def _build_llm_audit_detail(
    db: AsyncSession,
    payload: LlmSystemConfigUpdate,
) -> str:
    """
    构造 LLM 配置更新审计详情，仅记录变更键名与秘密字段操作。

    Args:
        db: 异步数据库会话
        payload: 已校验的更新载荷

    Returns:
        不含 API Key 明文的审计详情
    """
    existing = await system_config_crud.get_by_keys(db, LLM_CONFIG_KEYS)
    new_values = {
        KEY_LLM_CHAT_BASE_URL: payload.chat_base_url,
        KEY_LLM_CHAT_MODEL: payload.chat_model,
        KEY_LLM_CHAT_INPUT_COST_PER_MILLION_USD: str(
            payload.chat_input_cost_per_million_usd
        ),
        KEY_LLM_CHAT_OUTPUT_COST_PER_MILLION_USD: str(
            payload.chat_output_cost_per_million_usd
        ),
        KEY_LLM_EMBEDDING_BASE_URL: payload.embedding_base_url,
        KEY_LLM_EMBEDDING_MODEL: payload.embedding_model,
    }
    changed_keys: list[str] = []
    for key, new_value in new_values.items():
        row = existing.get(key)
        old_value = row.value if row is not None else None
        if old_value != new_value:
            changed_keys.append(key)

    parts: list[str] = []
    if changed_keys:
        parts.append(f"变更键: {', '.join(changed_keys)}")

    secret_parts: list[str] = []
    if payload.clear_chat_api_key:
        secret_parts.append("chat_api_key=已清空")
    elif payload.chat_api_key is not None:
        secret_parts.append("chat_api_key=已替换")
    if payload.clear_embedding_api_key:
        secret_parts.append("embedding_api_key=已清空")
    elif payload.embedding_api_key is not None:
        secret_parts.append("embedding_api_key=已替换")
    if secret_parts:
        parts.append("; ".join(secret_parts))

    return "; ".join(parts) if parts else "无变更"


async def _build_operations_audit_detail(
    db: AsyncSession,
    payload: OperationsSystemConfigUpdate,
) -> str:
    """
    构造运行参数更新审计详情，仅记录变更键名。

    Args:
        db: 异步数据库会话
        payload: 已校验的更新载荷

    Returns:
        审计详情字符串
    """
    existing = await system_config_crud.get_by_keys(db, OPERATIONS_CONFIG_KEYS)
    new_values = {
        KEY_HITL_NOTIFY_AUTO_APPROVE: (
            "true" if payload.hitl_notify_auto_approve else "false"
        ),
        KEY_MONITOR_PROBE_TIMEOUT_SECONDS: str(payload.monitor_probe_timeout_seconds),
        KEY_MONITOR_SWEEP_INTERVAL_SECONDS: str(payload.monitor_sweep_interval_seconds),
        KEY_CMDB_DIFF_INTERVAL_SECONDS: str(payload.cmdb_diff_interval_seconds),
        KEY_MONITOR_EVENT_RETENTION_DAYS: str(payload.monitor_event_retention_days),
    }
    changed_keys: list[str] = []
    for key, new_value in new_values.items():
        row = existing.get(key)
        old_value = row.value if row is not None else None
        if old_value != new_value:
            changed_keys.append(key)
    if not changed_keys:
        return "无变更"
    return f"变更键: {', '.join(changed_keys)}"


async def _load_system_config_response(db: AsyncSession) -> SystemConfigResponse:
    """
    加载脱敏系统配置响应，并转换解密类错误为安全 HTTP 响应。

    Args:
        db: 异步数据库会话

    Returns:
        脱敏后的系统配置响应

    Raises:
        HTTPException: 加密密钥缺失时返回 422；密文无法解密时返回 500
    """
    try:
        return await build_system_config_response(db)
    except DataEncryptionKeyMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_ENCRYPTION_KEY_MISSING_DETAIL,
        ) from exc
    except DataDecryptError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_DECRYPT_ERROR_DETAIL,
        ) from exc


@router.get("", response_model=ResponseEnvelope[SystemConfigResponse])
async def get_system_config(
    response: Response,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("system_config:manage")),
) -> ResponseEnvelope[SystemConfigResponse]:
    """
    读取当前有效系统配置（脱敏）。

    Returns:
        不含 API Key 明文或密文的系统配置
    """
    _set_no_store(response)
    data = await _load_system_config_response(db)
    return success_response(data)


@router.put("/llm", response_model=ResponseEnvelope[SystemConfigResponse])
async def update_llm_system_config(
    payload: LlmSystemConfigUpdate,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("system_config:manage")),
) -> ResponseEnvelope[SystemConfigResponse]:
    """
    更新 LLM 与 Embedding 运行配置。

    Args:
        payload: 模型与 API Key 更新载荷
        request: 当前 HTTP 请求（用于审计 IP）
        response: 用于设置 no-store 响应头
        db: 异步数据库会话
        current_user: 当前已授权用户

    Returns:
        更新后的脱敏系统配置
    """
    _set_no_store(response)
    audit_detail = await _build_llm_audit_detail(db, payload)
    try:
        await save_llm_config(
            db,
            payload,
            updated_by_user_id=current_user.id,
        )
    except DataEncryptionKeyMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_ENCRYPTION_KEY_MISSING_DETAIL,
        ) from exc

    await log_audit(
        db,
        user_id=current_user.id,
        action="update_llm_system_config",
        target="system_config:llm",
        detail=audit_detail,
        ip=get_client_ip(request),
    )
    await db.commit()
    data = await _load_system_config_response(db)
    return success_response(data, message="更新成功")


@router.put("/operations", response_model=ResponseEnvelope[SystemConfigResponse])
async def update_operations_system_config(
    payload: OperationsSystemConfigUpdate,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("system_config:manage")),
) -> ResponseEnvelope[SystemConfigResponse]:
    """
    更新 HITL 与监控运行参数。

    Args:
        payload: 运行参数更新载荷
        request: 当前 HTTP 请求（用于审计 IP）
        response: 用于设置 no-store 响应头
        db: 异步数据库会话
        current_user: 当前已授权用户

    Returns:
        更新后的脱敏系统配置
    """
    _set_no_store(response)
    audit_detail = await _build_operations_audit_detail(db, payload)
    await save_operations_config(
        db,
        payload,
        updated_by_user_id=current_user.id,
    )
    await log_audit(
        db,
        user_id=current_user.id,
        action="update_operations_system_config",
        target="system_config:operations",
        detail=audit_detail,
        ip=get_client_ip(request),
    )
    await db.commit()
    data = await _load_system_config_response(db)
    return success_response(data, message="更新成功")
