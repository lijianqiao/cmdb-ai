"""CRUD 包初始化，导出所有 CRUD 实例。"""

from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.crud.audit_log import audit_log_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.cmdb_asset_dependency import cmdb_asset_dependency_crud
from app.crud.dashboard import dashboard_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.crud.permission import permission_crud
from app.crud.role import role_crud
from app.crud.user import user_crud

__all__ = [
    "agent_message_crud",
    "agent_registry_crud",
    "agent_session_crud",
    "agent_trace_event_crud",
    "audit_log_crud",
    "cmdb_asset_crud",
    "cmdb_asset_dependency_crud",
    "dashboard_crud",
    "hitl_proposal_crud",
    "knowledge_category_crud",
    "knowledge_chunk_crud",
    "knowledge_document_crud",
    "monitor_status_event_crud",
    "monitor_target_crud",
    "permission_crud",
    "role_crud",
    "user_crud",
]
