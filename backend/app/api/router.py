"""API 路由聚合器。

将所有 v1 子路由注册到统一的前缀下。
"""

from fastapi import APIRouter

from app.api.v1.agent_sessions import router as agent_sessions_router
from app.api.v1.agent_ws import router as agent_ws_router
from app.api.v1.audit_logs import router as audit_logs_router
from app.api.v1.auth import router as auth_router
from app.api.v1.cmdb import router as cmdb_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.device_command_policies import router as device_command_policies_router
from app.api.v1.hitl import router as hitl_router
from app.api.v1.knowledge import router as knowledge_router
from app.api.v1.me import router as me_router
from app.api.v1.permissions import router as permissions_router
from app.api.v1.roles import router as roles_router
from app.api.v1.users import router as users_router

api_router = APIRouter()

# 注册子路由
api_router.include_router(auth_router, prefix="/auth", tags=["认证"])
api_router.include_router(users_router, prefix="/users", tags=["用户管理"])
api_router.include_router(roles_router, prefix="/roles", tags=["角色管理"])
api_router.include_router(permissions_router, prefix="/permissions", tags=["权限管理"])
api_router.include_router(me_router, prefix="/me", tags=["个人中心"])
api_router.include_router(dashboard_router, prefix="/dashboard", tags=["仪表盘"])
api_router.include_router(audit_logs_router, prefix="/audit-logs", tags=["审计日志"])
api_router.include_router(cmdb_router, prefix="/cmdb", tags=["CMDB 资产"])
api_router.include_router(
    device_command_policies_router,
    prefix="/device-command-policies",
    tags=["设备命令策略"],
)
api_router.include_router(knowledge_router, prefix="/knowledge", tags=["知识库"])
api_router.include_router(hitl_router, prefix="/hitl", tags=["HITL 审批"])
api_router.include_router(agent_sessions_router, prefix="/agent", tags=["Agent 会话"])
api_router.include_router(agent_ws_router, prefix="/ws", tags=["Agent WebSocket"])
