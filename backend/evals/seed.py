"""重建 eval 测试库：建表 → 灌 DB 行 → 写磁盘文件。

实现流程：
1. eval 要能重复跑出可比的分数，所以每轮开头把测试库整个推平重建，再灌一批
   **内容写死、主键写死**的数据。主键写死是为了让用例能直接断言「SW-01」
   「阈值 60%」这种具体值，而不是去猜自增出来的 ID 是几。
2. 知识库的种子有两半：数据库里的 KnowledgeDocument 行，和磁盘上的 .md 文件。
   两边的路径**必须由同一个函数产出**——这里直接调
   `knowledge_storage.write_document_file()` 让它决定路径，再把返回的相对路径
   写进 DB 行。这个仓库出过一次两边漂移的 bug（commit d76bdc1：改分类没搬文件，
   kb_grep 认旧目录、向量检索认新分类，两条检索路径对同一份文档给出相反答案）。
   路径约定只留一处就不会再漂。
3. 向量在 seed_embeddings 里补（要连本机 embedding 服务），这一步只管
   DB 行和磁盘文件，好让不依赖模型的部分能被普通 pytest 覆盖。
"""

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models.base import Base
from app.models.cmdb_asset import CmdbAsset
from app.models.cmdb_asset_dependency import CmdbAssetDependency
from app.models.knowledge_category import KnowledgeCategory
from app.models.knowledge_document import KnowledgeDocument
from app.models.monitor_status_event import MonitorStatusEvent
from app.models.monitor_target import MonitorTarget
from app.services import knowledge_storage
from evals.config import EvalPaths


@dataclass(frozen=True, slots=True)
class SeedDocument:
    """一份种子文档：清单在这里，正文在 fixtures/ 磁盘上。"""

    doc_id: int
    category_code: str
    title: str
    filename: str


SEED_CATEGORIES: tuple[tuple[int, str, str], ...] = (
    (1, "network", "网络"),
    (2, "server", "服务器"),
    (3, "policy", "制度"),
)

# 前两份内容**刻意相似**（现行 v3 说 CPU 阈值 60%，已废弃 v1 说 90%），
# kb-disambiguate 用例靠这个分歧判别模型有没有拿错版本。
SEED_DOCUMENTS: tuple[SeedDocument, ...] = (
    SeedDocument(1, "network", "交换机例行巡检规程（现行 v3）", "switch-inspection.md"),
    SeedDocument(
        2, "network", "交换机例行巡检规程（v1 已废弃）", "switch-inspection-legacy.md"
    ),
    SeedDocument(3, "network", "VLAN 划分规范", "vlan-config.md"),
    SeedDocument(4, "server", "Linux 磁盘空间清理步骤", "linux-disk-cleanup.md"),
    SeedDocument(5, "server", "服务重启标准流程", "service-restart.md"),
    SeedDocument(6, "policy", "变更窗口规定", "change-window.md"),
)

# (id, hostname, ip)
SEED_ASSETS: tuple[tuple[int, str, str], ...] = (
    (1, "SW-01", "10.0.30.1"),
    (2, "SW-02", "10.0.30.2"),
    (3, "SRV-01", "10.0.20.11"),
    (4, "SRV-02", "10.0.20.12"),
    (5, "FW-01", "10.0.30.254"),
)

# 依赖链 SW-01 → SW-02 → SRV-01/SRV-02。cmdb-dependency 用例靠它测
# 「模型会不会偷懒只查一层就瞎答」——只调 query_cmdb 是查不出下游的。
SEED_DEPENDENCIES: tuple[tuple[int, int], ...] = ((1, 2), (2, 3), (2, 4))

# (监控目标 id, 设备 id, 状态, 延迟)。SRV-02 故意是 down，供告警类提问使用。
SEED_MONITORS: tuple[tuple[int, int, str, int | None], ...] = (
    (1, 1, "up", 2),
    (2, 3, "up", 5),
    (3, 4, "down", None),
)


# 建表的前置条件：vector 撑 knowledge_chunks.embedding，
# pg_trgm 撑 permissions 等表上的 gin_trgm_ops 索引。
# 两者都在 alembic 迁移里创建，但 eval 用 create_all 直接建表、不跑迁移，
# 所以得在这里自己补上——少一个就会在建索引时报
# `operator class "gin_trgm_ops" does not exist`。
_REQUIRED_EXTENSIONS = ("vector", "pg_trgm")


async def reset_schema(engine: AsyncEngine) -> None:
    """把测试库推平重建。

    用 create_all 而不是跑 alembic：测试库是一次性的，不需要迁移历史，
    直接按当前模型建表更快也更不容易受历史迁移影响。

    **只允许在 eval 专用库上调用。** 调用方负责保证 engine 指向的是 5434 上的
    那个独立容器——这也是 config.eval_database_url() 默认值写死的原因。
    """
    async with engine.begin() as conn:
        for extension in _REQUIRED_EXTENSIONS:
            await conn.exec_driver_sql(f"CREATE EXTENSION IF NOT EXISTS {extension}")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


def clear_workdir(paths: EvalPaths) -> None:
    """删掉上一轮的磁盘产物，否则残留文件会被 kb_grep 扫到，污染检索结果。"""
    for root in (paths.knowledge_root, paths.knowledge_trash_root):
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)


def read_fixtures(paths: EvalPaths) -> dict[int, bytes]:
    """一次性把 6 份 fixture 正文读进内存，按 doc_id 索引。

    单独抽成同步函数，而不是在 seed_all 里边循环边读：文件 I/O 是阻塞的，
    放在 async 函数里会占住事件循环（ruff ASYNC240 就是查这个）。
    顺带也把「读文件」和「写数据库」两件事分开了。
    """
    knowledge_dir = paths.fixtures_dir / "knowledge"
    contents: dict[int, bytes] = {}
    for doc in SEED_DOCUMENTS:
        source: Path = knowledge_dir / doc.category_code / doc.filename
        contents[doc.doc_id] = source.read_bytes()
    return contents


def split_into_chunks(text: str, *, max_chars: int = 800) -> list[str]:
    """把正文按固定字符数切片，滤掉纯空白的片。

    刻意用最笨的定长切法：种子文档是我们自己写的、每份只有几百字，
    上智能分段没有收益，反而多一处会漂的行为——而种子一漂，用例的
    正确答案就跟着变了，防回归就无从谈起。

    纯空白的片必须滤掉：embedding 接口收到空字符串会直接报错。
    """
    chunks = [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
    return [chunk for chunk in chunks if chunk.strip()]


async def seed_embeddings(session: AsyncSession, paths: EvalPaths) -> None:
    """给每份种子文档算向量并写进 knowledge_chunks，kb_semantic_search 才有东西可检。

    走 app.core.llm.embed 这个统一入口，不自建 OpenAI 客户端。
    embedding 模型在本机 127.0.0.1:8080，免费，但**必须先把它起起来**——
    这也正是 eval 跑在宿主机而不是容器里的原因：容器里的 127.0.0.1 是容器自己。
    """
    from app.core.llm import embed
    from app.models.knowledge_chunk import KnowledgeChunk

    contents = read_fixtures(paths)
    for doc in SEED_DOCUMENTS:
        chunks = split_into_chunks(contents[doc.doc_id].decode("utf-8"))
        result = await embed("local-embedding", chunks)
        for index, (chunk, vector) in enumerate(
            zip(chunks, result.vectors, strict=True)
        ):
            session.add(
                KnowledgeChunk(
                    document_id=doc.doc_id,
                    chunk_index=index,
                    content=chunk,
                    token_count=len(chunk),
                    embedding=vector,
                )
            )
    await session.flush()


async def seed_all(session: AsyncSession, paths: EvalPaths) -> None:
    """灌入全部种子：分类、文档（DB 行 + 磁盘文件）、设备、依赖、监控。"""
    clear_workdir(paths)

    for category_id, code, name in SEED_CATEGORIES:
        session.add(KnowledgeCategory(id=category_id, code=code, name=name))
    await session.flush()

    category_id_by_code = {code: category_id for category_id, code, _ in SEED_CATEGORIES}
    fixture_contents = read_fixtures(paths)

    for doc in SEED_DOCUMENTS:
        content = fixture_contents[doc.doc_id]
        # 路径由 write_document_file 决定，DB 行只记它的返回值——两边共用一个
        # 来源，就不会出现「库里说在 A、磁盘上在 B」这种检索路径打架的情况。
        relative_path = knowledge_storage.write_document_file(
            category_code=doc.category_code,
            document_id=doc.doc_id,
            filename=doc.filename,
            content=content,
        )
        session.add(
            KnowledgeDocument(
                id=doc.doc_id,
                category_id=category_id_by_code[doc.category_code],
                title=doc.title,
                original_filename=doc.filename,
                file_path=relative_path,
                file_type="md",
                content_hash=hashlib.sha256(content).hexdigest(),
                status="ready",
            )
        )

    for asset_id, hostname, ip in SEED_ASSETS:
        session.add(
            CmdbAsset(
                id=asset_id,
                asset_type="server" if hostname.startswith("SRV") else "switch",
                hostname=hostname,
                ip_address=ip,
                location="机房 A",
                business_system="核心网",
                vendor="h3c",
            )
        )
    await session.flush()

    for parent_id, child_id in SEED_DEPENDENCIES:
        session.add(
            CmdbAssetDependency(
                parent_asset_id=parent_id,
                child_asset_id=child_id,
                relation_type="uplink",
            )
        )

    ip_by_asset_id = {asset_id: ip for asset_id, _, ip in SEED_ASSETS}
    hostname_by_asset_id = {asset_id: name for asset_id, name, _ in SEED_ASSETS}
    # 目标和事件分两轮加：事件的外键指向目标，目标必须先 flush 拿到主键。
    for target_id, asset_id, _status, _latency in SEED_MONITORS:
        session.add(
            MonitorTarget(
                id=target_id,
                cmdb_asset_id=asset_id,
                ip_address=ip_by_asset_id[asset_id],
                port=22,
                label=hostname_by_asset_id[asset_id],
            )
        )
    await session.flush()

    for target_id, _asset_id, status, latency in SEED_MONITORS:
        session.add(
            MonitorStatusEvent(
                target_id=target_id,
                status=status,
                latency_ms=latency,
                detail="" if status == "up" else "连续 3 次探测失败",
            )
        )

    await session.flush()
