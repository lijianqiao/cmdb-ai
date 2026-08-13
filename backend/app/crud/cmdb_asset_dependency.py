"""CRUD operations for the CMDB asset dependency graph.

Not a CRUDBase subclass: this model has a composite primary key, not an
`id` column, so CRUDBase's `_id_column()` machinery does not apply.
"""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cmdb_asset_dependency import CmdbAssetDependency


class CRUDCmdbAssetDependency:
    """Dependency-edge persistence and breadth-first graph traversal."""

    model = CmdbAssetDependency

    async def create(
        self,
        db: AsyncSession,
        *,
        parent_asset_id: int,
        child_asset_id: int,
        relation_type: str,
    ) -> CmdbAssetDependency:
        """Add one dependency edge and flush."""
        edge = CmdbAssetDependency(
            parent_asset_id=parent_asset_id,
            child_asset_id=child_asset_id,
            relation_type=relation_type,
        )
        db.add(edge)
        await db.flush()
        return edge

    async def remove(
        self, db: AsyncSession, *, parent_asset_id: int, child_asset_id: int
    ) -> bool:
        """Delete one dependency edge; return whether a row was actually removed."""
        stmt = select(CmdbAssetDependency).where(
            CmdbAssetDependency.parent_asset_id == parent_asset_id,
            CmdbAssetDependency.child_asset_id == child_asset_id,
        )
        edge = (await db.execute(stmt)).scalar_one_or_none()
        if edge is None:
            return False
        await db.delete(edge)
        await db.flush()
        return True

    async def get_children(self, db: AsyncSession, parent_asset_id: int) -> list[CmdbAssetDependency]:
        """Return every edge where `parent_asset_id` is the parent."""
        stmt = select(CmdbAssetDependency).where(
            CmdbAssetDependency.parent_asset_id == parent_asset_id
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_parents(self, db: AsyncSession, child_asset_id: int) -> list[CmdbAssetDependency]:
        """Return every edge where `child_asset_id` is the child."""
        stmt = select(CmdbAssetDependency).where(
            CmdbAssetDependency.child_asset_id == child_asset_id
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def traverse(
        self,
        db: AsyncSession,
        asset_id: int,
        *,
        direction: Literal["up", "down"],
        max_depth: int = 3,
    ) -> list[tuple[int, int]]:
        """Breadth-first traverse the dependency graph from `asset_id`.

        `direction="down"` follows parent->child edges; `direction="up"`
        follows child->parent edges. Returns (asset_id, depth) pairs,
        excluding the starting asset, cycle-safe, capped at `max_depth`.
        """
        visited: set[int] = {asset_id}
        frontier: list[int] = [asset_id]
        results: list[tuple[int, int]] = []
        depth = 0

        while frontier and depth < max_depth:
            depth += 1
            next_frontier: list[int] = []
            for current_id in frontier:
                if direction == "down":
                    edges = await self.get_children(db, current_id)
                    neighbor_ids = [edge.child_asset_id for edge in edges]
                else:
                    edges = await self.get_parents(db, current_id)
                    neighbor_ids = [edge.parent_asset_id for edge in edges]

                for neighbor_id in neighbor_ids:
                    if neighbor_id in visited:
                        continue
                    visited.add(neighbor_id)
                    results.append((neighbor_id, depth))
                    next_frontier.append(neighbor_id)
            frontier = next_frontier

        return results


cmdb_asset_dependency_crud = CRUDCmdbAssetDependency()
