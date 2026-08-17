"""Agent-facing read-only tools for CMDB and monitoring (docs/AGENT_ARCHITECTURE.md §4.2).

All three tools need `db: AsyncSession` (unlike T07's filesystem-backed
kb_glob/kb_read/kb_grep) since they query structured data, matching
kb_semantic_search's precedent from T07. None of them are wired into a real
ToolDispatcher closure yet — that lands with whichever task first invokes
app.agent.loop.run_loop for real (see this plan's header).
"""

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import ToolResult
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.cmdb_asset_dependency import cmdb_asset_dependency_crud
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.models.cmdb_asset import CmdbAsset
from app.models.monitor_target import MonitorTarget

# 工具结果的字符预算。要控的是灌进模型上下文的 token 量，所以按字符算而不是
# 按行数算——notes/location 是自由文本，单行长度能差一个数量级，按行数截断在
# 备注写得长的环境里根本挡不住。
_MAX_TOOL_RESULT_CHARS = 8000
_MAX_NOTES_CHARS = 100


def _format_asset(asset: CmdbAsset) -> str:
    notes = asset.notes or "无"
    if len(notes) > _MAX_NOTES_CHARS:
        notes = notes[:_MAX_NOTES_CHARS] + "…"
    return (
        f"[id={asset.id}] {asset.hostname} ({asset.ip_address}) "
        f"类型={asset.asset_type} 位置={asset.location or '未填写'} "
        f"业务系统={asset.business_system or '未填写'} 备注={notes}"
    )


def _render_assets(assets: list[CmdbAsset]) -> str:
    """在字符预算内渲染资产列表，超出时截断并提示模型收窄条件。"""
    lines: list[str] = []
    used = 0
    for asset in assets:
        line = _format_asset(asset)
        if used + len(line) > _MAX_TOOL_RESULT_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    if len(lines) < len(assets):
        lines.append(
            f"…共匹配 {len(assets)} 台，因长度限制仅显示前 {len(lines)} 台。"
            "请补充更精确的过滤条件（IP 或业务系统）后重试。"
        )
    return "\n".join(lines)


async def query_cmdb(
    db: AsyncSession,
    *,
    asset_ids: list[int] | None = None,
    ip: str | None = None,
    business_system: str | None = None,
) -> ToolResult:
    """Look up CMDB assets by id list, IP, or business system; no filter returns everything."""
    if asset_ids is not None:
        assets = await cmdb_asset_crud.list_by_ids(db, asset_ids)
    elif ip is not None:
        found = await cmdb_asset_crud.get_by_ip(db, ip)
        assets = [found] if found is not None else []
    elif business_system is not None:
        assets = await cmdb_asset_crud.list_by_business_system(db, business_system)
    else:
        assets = await cmdb_asset_crud.list_all(db)

    if not assets:
        return ToolResult(control="ok", content="没有找到匹配的资产")
    return ToolResult(control="ok", content=_render_assets(assets))


async def query_cmdb_dependencies(
    db: AsyncSession,
    asset_id: int,
    *,
    direction: Literal["up", "down"] = "down",
    max_depth: int = 3,
) -> ToolResult:
    """Traverse the CMDB dependency graph from `asset_id`."""
    reached = await cmdb_asset_dependency_crud.traverse(
        db, asset_id, direction=direction, max_depth=max_depth
    )
    if not reached:
        return ToolResult(control="ok", content="没有找到依赖关系")

    reached_ids = [asset_id for asset_id, _depth in reached]
    assets_by_id = {a.id: a for a in await cmdb_asset_crud.list_by_ids(db, reached_ids)}
    depth_by_id = dict(reached)

    # max_depth=5 的依赖遍历同样可能命中大量资产，这里也走字符预算。
    lines: list[str] = []
    used = 0
    for a_id in reached_ids:
        asset = assets_by_id.get(a_id)
        if asset is None:
            continue
        line = f"[深度={depth_by_id[a_id]}] {_format_asset(asset)}"
        if used + len(line) > _MAX_TOOL_RESULT_CHARS:
            lines.append(f"…共 {len(reached_ids)} 个关联资产，因长度限制已截断，请缩小 max_depth。")
            break
        lines.append(line)
        used += len(line) + 1
    return ToolResult(control="ok", content="\n".join(lines))


async def query_monitor_status(
    db: AsyncSession,
    *,
    target_ids: list[int] | None = None,
    ip_prefix: str | None = None,
    since_limit: int = 5,
) -> ToolResult:
    """Report each target's current status derived from its latest event and recent history.

    ``ip_prefix`` is a literal string-prefix match, not CIDR arithmetic. The
    name deliberately matches ``monitor_target_crud.list_by_ip_prefix``'s
    behavior instead of promising unsupported CIDR semantics.
    """
    targets: list[MonitorTarget]
    if target_ids is not None:
        # 批量 IN 查询，不要逐个 get——target_ids 上限 100，逐条会打 100 次往返。
        targets = await monitor_target_crud.list_by_ids(db, target_ids)
    elif ip_prefix is not None:
        targets = await monitor_target_crud.list_by_ip_prefix(db, ip_prefix)
    else:
        targets = await monitor_target_crud.list_active(db)

    if not targets:
        return ToolResult(control="ok", content="没有找到匹配的监控目标")

    target_id_list = [target.id for target in targets]
    latest_status = await monitor_status_event_crud.get_latest_status_for_targets(
        db,
        target_id_list,
    )
    # 一次窗口查询取回所有目标的最近历史，取代「每个目标一次 list_recent_for_target」。
    # 无过滤条件时 targets 是全系统目标，逐条查会打出与目标数等量的往返。
    recent_by_target = await monitor_status_event_crud.list_recent_for_targets(
        db,
        target_id_list,
        limit=since_limit,
    )

    lines: list[str] = []
    for target in targets:
        header = f"[id={target.id}] {target.ip_address}:{target.port} ({target.label or '未命名'})"
        latest = latest_status.get(target.id)
        if latest is None:
            lines.append(f"{header} — 尚未探测")
            continue

        recent = recent_by_target.get(target.id, [])
        history = ", ".join(f"{event.status}@{event.checked_at:%H:%M:%S}" for event in recent)
        latency_text = f"{latest.latency_ms}ms" if latest.latency_ms is not None else "—"
        lines.append(
            f"{header} — 当前: {latest.status} (延迟 {latency_text}); 最近记录: {history}"
        )

    return ToolResult(control="ok", content="\n".join(lines))
