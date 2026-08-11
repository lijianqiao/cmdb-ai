"""Allowlist, JSON-schema, and argument-validation tests for child tools."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import tool_dispatch
from app.agent.loop import ToolResult
from app.agent.tool_dispatch import build_tool_dispatcher, tool_schemas_for


def test_tool_schemas_expose_only_requested_names() -> None:
    schemas = tool_schemas_for(("kb_read", "query_monitor_status"))

    assert [schema["function"]["name"] for schema in schemas] == [
        "kb_read",
        "query_monitor_status",
    ]
    for schema in schemas:
        assert schema["type"] == "function"
        assert schema["function"]["parameters"]["additionalProperties"] is False


def test_all_seven_registered_tools_have_strict_schemas() -> None:
    names = (
        "kb_glob",
        "kb_grep",
        "kb_read",
        "kb_semantic_search",
        "query_cmdb",
        "query_cmdb_dependencies",
        "query_monitor_status",
    )

    schemas = tool_schemas_for(names)

    assert [item["function"]["name"] for item in schemas] == list(names)
    assert all(
        item["function"]["parameters"]["additionalProperties"] is False
        for item in schemas
    )


async def test_dispatch_rejects_tool_outside_allowlist(db_session: AsyncSession) -> None:
    dispatch = build_tool_dispatcher(db_session, ("kb_read",))

    result = await dispatch("query_cmdb", {})

    assert result.control == "rejected"
    assert "不在角色白名单" in result.content


async def test_dispatch_rejects_unknown_tool_even_if_persisted_allowlist_contains_it(
    db_session: AsyncSession,
) -> None:
    dispatch = build_tool_dispatcher(db_session, ("unknown_tool",))

    result = await dispatch("unknown_tool", {})

    assert result.control == "rejected"
    assert "未知工具" in result.content


async def test_dispatch_requests_clarification_for_invalid_arguments(
    db_session: AsyncSession,
) -> None:
    dispatch = build_tool_dispatcher(db_session, ("kb_read",))

    result = await dispatch("kb_read", {"path": "sop.md", "offset": -1})

    assert result.control == "clarification"
    assert "参数无效" in result.content


async def test_dispatch_does_not_coerce_argument_types(db_session: AsyncSession) -> None:
    dispatch = build_tool_dispatcher(db_session, ("kb_read",))

    result = await dispatch("kb_read", {"path": "sop.md", "offset": "1"})

    assert result.control == "clarification"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("kb_glob", {"pattern": ""}),
        ("kb_grep", {"pattern": "x", "context_lines": 21}),
        ("kb_read", {"path": "x", "unexpected": True}),
        ("kb_semantic_search", {"query": "x", "top_k": 21}),
        ("query_cmdb", {"ip": "10.0.0.1", "business_system": "ops"}),
        ("query_cmdb_dependencies", {"asset_id": 1, "max_depth": 6}),
        ("query_monitor_status", {"target_ids": [1], "ip_prefix": "10."}),
    ],
)
async def test_every_tool_rejects_its_boundary_violation(
    db_session: AsyncSession, tool_name: str, arguments: dict[str, Any]
) -> None:
    dispatch = build_tool_dispatcher(db_session, (tool_name,))

    result = await dispatch(tool_name, arguments)

    assert result.control == "clarification"


async def test_dispatch_calls_validated_knowledge_tool(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_kb_read(path: str, *, offset: int, limit: int | None) -> ToolResult:
        captured.update(path=path, offset=offset, limit=limit)
        return ToolResult(control="ok", content="document")

    monkeypatch.setattr(tool_dispatch, "kb_read", fake_kb_read)
    dispatch = build_tool_dispatcher(db_session, ("kb_read",))

    result = await dispatch("kb_read", {"path": "sop/a.md", "limit": 100})

    assert result == ToolResult(control="ok", content="document")
    assert captured == {"path": "sop/a.md", "offset": 0, "limit": 100}


async def test_dispatch_calls_validated_db_tool(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_query_cmdb(
        db: AsyncSession,
        *,
        asset_ids: list[int] | None,
        ip: str | None,
        business_system: str | None,
    ) -> ToolResult:
        assert db is db_session
        captured.update(
            asset_ids=asset_ids,
            ip=ip,
            business_system=business_system,
        )
        return ToolResult(control="ok", content="asset")

    monkeypatch.setattr(tool_dispatch, "query_cmdb", fake_query_cmdb)
    dispatch = build_tool_dispatcher(db_session, ("query_cmdb",))

    result = await dispatch("query_cmdb", {"ip": "10.0.0.5"})

    assert result.control == "ok"
    assert captured == {
        "asset_ids": None,
        "ip": "10.0.0.5",
        "business_system": None,
    }


@pytest.mark.parametrize(
    ("tool_name", "arguments", "target", "expected_args", "expected_kwargs"),
    [
        ("kb_glob", {"pattern": "*.md"}, "kb_glob", ("*.md",), {"category": None}),
        ("kb_grep", {"pattern": "incident"}, "kb_grep", ("incident",), {"category": None, "context_lines": 0}),
        (
            "kb_semantic_search",
            {"query": "incident"},
            "kb_semantic_search",
            ("db_session", "incident"),
            {"category_id": None, "top_k": 5},
        ),
        (
            "query_cmdb_dependencies",
            {"asset_id": 7},
            "query_cmdb_dependencies",
            ("db_session", 7),
            {"direction": "down", "max_depth": 3},
        ),
        (
            "query_monitor_status",
            {},
            "query_monitor_status",
            ("db_session",),
            {"target_ids": None, "ip_prefix": None, "since_limit": 5},
        ),
    ],
)
async def test_dispatch_calls_validated_remaining_tools(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, Any],
    target: str,
    expected_args: tuple[object, ...],
    expected_kwargs: dict[str, object],
) -> None:
    captured: dict[str, object] = {}

    async def fake_tool(*args: object, **kwargs: object) -> ToolResult:
        captured.update(args=args, kwargs=kwargs)
        return ToolResult(control="ok", content="result")

    monkeypatch.setattr(tool_dispatch, target, fake_tool)
    dispatch = build_tool_dispatcher(db_session, (tool_name,))

    result = await dispatch(tool_name, arguments)

    expected_bound_args = tuple(
        db_session if value == "db_session" else value for value in expected_args
    )
    assert result == ToolResult(control="ok", content="result")
    assert captured == {"args": expected_bound_args, "kwargs": expected_kwargs}


async def test_dispatch_hides_internal_exception_detail(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_kb_read(path: str, *, offset: int, limit: int | None) -> ToolResult:
        raise RuntimeError("secret database address")

    monkeypatch.setattr(tool_dispatch, "kb_read", broken_kb_read)
    dispatch = build_tool_dispatcher(db_session, ("kb_read",))

    result = await dispatch("kb_read", {"path": "sop/a.md"})

    assert result.control == "failed"
    assert "RuntimeError" in result.content
    assert "secret database address" not in result.content
