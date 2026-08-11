"""Contract tests for the code-owned child-Agent role catalog."""

from dataclasses import FrozenInstanceError

import pytest

from app.agent.roles import ROLE_CATALOG, UnknownAgentRoleError, get_role, list_roles


def test_catalog_contains_all_architecture_roles() -> None:
    assert tuple(ROLE_CATALOG) == (
        "classifier",
        "kb_explorer",
        "ops_explorer",
        "investigator",
        "reviewer",
    )


def test_every_role_is_versioned_described_and_read_only() -> None:
    for role in list_roles():
        assert role.version == "t09-v1"
        assert len(role.description) >= 20
        assert len(role.instructions) >= 80
        assert role.model_key == "local-chat"
        assert role.sandbox_mode == "read-only"


def test_role_tool_boundaries_are_least_privilege() -> None:
    knowledge = {"kb_glob", "kb_grep", "kb_read", "kb_semantic_search"}
    ops = {"query_cmdb", "query_cmdb_dependencies", "query_monitor_status"}

    assert set(get_role("classifier").tools_allowlist) == {
        "kb_glob",
        "kb_grep",
        "kb_read",
    }
    assert set(get_role("kb_explorer").tools_allowlist) == knowledge
    assert set(get_role("ops_explorer").tools_allowlist) == ops
    assert set(get_role("investigator").tools_allowlist) == knowledge | ops
    assert set(get_role("reviewer").tools_allowlist) == knowledge | ops


def test_role_model_tiers_match_architecture() -> None:
    assert get_role("classifier").model_tier == "fast"
    assert get_role("kb_explorer").model_tier == "fast"
    assert get_role("ops_explorer").model_tier == "fast"
    assert get_role("investigator").model_tier == "balanced"
    assert get_role("reviewer").model_tier == "reasoning"


def test_unknown_role_fails_closed() -> None:
    with pytest.raises(UnknownAgentRoleError, match="unknown child-Agent role"):
        get_role("worker")


def test_catalog_and_role_definitions_are_immutable() -> None:
    with pytest.raises(TypeError):
        ROLE_CATALOG["classifier"] = get_role("reviewer")  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        get_role("classifier").model_key = "other"  # type: ignore[misc]
