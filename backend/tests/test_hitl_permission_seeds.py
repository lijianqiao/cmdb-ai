"""HITL / ops permission seed contract."""

from init_db import SEED_PERMISSIONS

REQUIRED = {
    "knowledge:read",
    "knowledge:upload",
    "knowledge:manage",
    "cmdb:read",
    "cmdb:manage",
    "monitor:read",
    "monitor:manage",
    "agent:hitl_approve",
}


def test_seed_permissions_include_t10_codes() -> None:
    codes = {item["code"] for item in SEED_PERMISSIONS}
    assert REQUIRED <= codes
    assert len(codes) == len(SEED_PERMISSIONS)  # no duplicate codes
