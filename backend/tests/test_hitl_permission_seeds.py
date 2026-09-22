"""HITL / ops permission seed contract."""

from init_db import SEED_PERMISSIONS

REQUIRED = {
    "knowledge:read",
    "knowledge:upload",
    "knowledge:manage",
    "cmdb:read",
    "cmdb:manage",
    "cmdb:credential_read",
    "monitor:read",
    "monitor:manage",
    "monitor_log:read",
    "agent:use",
    "agent:hitl_approve",
    "agent:auto_execute",
    "system_config:manage",
}


def test_seed_permissions_include_t10_codes() -> None:
    codes = {item["code"] for item in SEED_PERMISSIONS}
    assert REQUIRED <= codes
    assert len(codes) == len(SEED_PERMISSIONS)  # no duplicate codes


def test_auto_execute_is_its_own_permission() -> None:
    """自动执行与人工审批是两种责任：独立权限码，归在 Agent 模块。"""
    by_code = {item["code"]: item for item in SEED_PERMISSIONS}
    auto_execute = by_code["agent:auto_execute"]
    assert auto_execute["module"] == "Agent"
    assert auto_execute["code"] != by_code["agent:hitl_approve"]["code"]
