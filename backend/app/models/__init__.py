"""模型包初始化，导出所有 ORM 模型。

供 Alembic autogenerate 和其他模块导入使用。
"""

from app.models.agent_message import AgentMessage
from app.models.agent_registry import AgentRegistry
from app.models.agent_session import AgentSession
from app.models.agent_trace_event import AgentTraceEvent
from app.models.audit_log import AuditLog
from app.models.base import Base
from app.models.cmdb_asset import CmdbAsset
from app.models.cmdb_asset_dependency import CmdbAssetDependency
from app.models.device_command_policy import DeviceCommandPolicy
from app.models.hitl_execution_result import HitlExecutionResult
from app.models.hitl_proposal import HitlProposal
from app.models.knowledge_category import KnowledgeCategory
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.monitor_status_event import MonitorStatusEvent
from app.models.monitor_target import MonitorTarget
from app.models.permission import Permission
from app.models.refresh_session import RefreshSession
from app.models.refresh_session_family import RefreshSessionFamily
from app.models.role import Role, role_permissions
from app.models.system_config import SystemConfig
from app.models.user import User, user_roles

__all__ = [
    "AgentMessage",
    "AgentRegistry",
    "AgentSession",
    "AgentTraceEvent",
    "AuditLog",
    "Base",
    "CmdbAsset",
    "CmdbAssetDependency",
    "DeviceCommandPolicy",
    "HitlExecutionResult",
    "HitlProposal",
    "KnowledgeCategory",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "MonitorStatusEvent",
    "MonitorTarget",
    "Permission",
    "RefreshSession",
    "RefreshSessionFamily",
    "Role",
    "SystemConfig",
    "User",
    "role_permissions",
    "user_roles",
]
