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
from app.models.cmdb_asset import CmdbAsset


def _format_asset(asset: CmdbAsset) -> str:
    return (
        f"[id={asset.id}] {asset.hostname} ({asset.ip_address}) "
        f"类型={asset.asset_type} 位置={asset.location or '未填写'} "
        f"业务系统={asset.business_system or '未填写'} 备注={asset.notes or '无'}"
    )


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
    return ToolResult(control="ok", content="\n".join(_format_asset(a) for a in assets))


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

    lines = [
        f"[深度={depth_by_id[a_id]}] {_format_asset(assets_by_id[a_id])}"
        for a_id in reached_ids
        if a_id in assets_by_id
    ]
    return ToolResult(control="ok", content="\n".join(lines))
