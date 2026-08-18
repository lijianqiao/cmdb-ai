"""知识文档 AI 分类建议。

实现流程：
1. 建议永远只是建议：本模块只写 `KnowledgeDocument.suggested_*` 字段，
   绝不直接改 `category_id`。真实归属的变更必须由人在管理页点「应用」触发
   （见 `crud.knowledge_document.apply_category`）。
2. 单份文档走 `_suggest_one`：直接调 `llm.chat`，**不 spawn 子 Agent**。
   架构文档明确规定单文档分类是单次动作，禁止 spawn（AGENT_ARCHITECTURE §5 反模式红线）。
3. 两份及以上走 `classify_documents` 编排工作流：它负责分波并发、结果解析与
   冲突复核。该工作流要求一个真实的 `AgentSession`（`AgentRegistry` 有外键），
   而本模块不在会话上下文里，所以临时建一个、跑完即删——建议已经落到文档行上，
   子 Agent 的对话记录没有保留价值。
4. 任何一份文档解析失败都不影响其它文档：失败的那份不写建议，调用方按
   「未获得建议」处理，不会把半个结果当成有效建议。

为什么不把建议直接写成分类：分类错误的代价是知识检索长期失准，而 kb_grep /
kb_semantic_search 的召回都依赖分类范围。让人过一眼的成本远低于事后发现检索
不到东西再回来排查。
"""

import json
import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.orchestration import ClassificationDocument, classify_documents
from app.agent.spawn import spawn_manager
from app.core.llm import ChatMessage, chat
from app.crud.agent_session import agent_session_crud
from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.models.knowledge_category import UNCATEGORIZED_CODE, KnowledgeCategory
from app.models.knowledge_document import KnowledgeDocument
from app.services.knowledge_storage import read_document_file

logger = logging.getLogger(__name__)

# 送给模型的正文上限。分类只需要看开头就能判断，全文进上下文纯属浪费预算；
# 运维 SOP 的主题几乎总在前几百字里说清楚。
_CLASSIFY_EXCERPT_CHARS = 3000

_SINGLE_DOC_SYSTEM_PROMPT = """你是运维知识文档分类器。
只根据给出的文档正文判断它属于哪个分类，只能从候选分类的 code 里选一个。
没有合适的候选时，把 category 设为空字符串。
只输出一个 JSON 对象，不要 Markdown 代码围栏，不要任何解释性文字：
{"category":"code 或空字符串","confidence":0到1的小数,"reason":"一句有正文依据的中文理由"}"""


type SuggestionKind = Literal["suggested", "unchanged", "no_match", "skipped"]


@dataclass(frozen=True, slots=True)
class SuggestionOutcome:
    """一次建议请求的结果统计，用于给调用方组装响应消息。"""

    suggested: int
    skipped: int
    # 模型认为当前分类就是对的。这不是失败，也不落库——落一条「建议 = 现分类」
    # 的记录只会在管理页留下一个点了没反应、又清不掉的死结。
    unchanged: int = 0
    # 模型明确回答「现有分类里没有合适的」。这同样不是失败，而是一条有用的结论：
    # 它告诉用户该新建一个分类。跟"调用失败/输出解析不了"混在一起报，
    # 用户只会以为分析挂了，完全看不出该去建分类。
    no_match: int = 0


def _category_menu(categories: list[KnowledgeCategory]) -> str:
    """把候选分类渲染成给模型看的清单。"""
    return "\n".join(
        f"- {category.code}: {category.name}"
        + (f"（{category.description}）" if category.description else "")
        for category in categories
    )


def _loads_json_object(text: str) -> dict[str, object] | None:
    """解析一个 JSON 对象，容忍模型在前后多写的解释文字。

    提示词已经写明「不要任何解释性文字」，但模型经常照说不误地加一句
    「根据文档内容，我认为……」再跟上 JSON。整串按 JSON 解析必然失败，
    而那次调用的钱已经花了、答案其实也给了——为这点格式洁癖丢掉结果不划算。

    只在整串解析失败后才退回到「取第一个 { 到最后一个 }」，
    格式规范的输出走的仍是原来那条路径。
    """
    try:
        payload = json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except ValueError:
            return None
    return payload if isinstance(payload, dict) else None


def _parse_single_suggestion(
    raw: str | None,
) -> tuple[str | None, float, str] | None:
    """解析单文档建议的 JSON 输出。

    Returns:
        None 表示输出无法解析；`(None, ...)` 表示模型明确回答「没有合适的分类」
        （提示词允许它把 category 留空），`(code, ...)` 表示给出了候选 code。
        这两种"没有 code"必须分开：前者是模型坏了，后者是模型认真回答了。
    """
    if not raw:
        return None
    text = raw.strip()
    # 小模型经常无视「不要代码围栏」的指令，这里主动剥一层再解析。
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0].strip()
    payload = _loads_json_object(text)
    if payload is None:
        return None
    code = payload.get("category")
    if not isinstance(code, str):
        return None
    raw_confidence = payload.get("confidence")
    confidence = (
        float(raw_confidence)
        if isinstance(raw_confidence, int | float) and not isinstance(raw_confidence, bool)
        else 0.0
    )
    confidence = min(max(confidence, 0.0), 1.0)
    reason = payload.get("reason")
    normalized_reason = reason.strip() if isinstance(reason, str) else ""
    return (code.strip() or None), confidence, normalized_reason


async def _suggest_one(
    db: AsyncSession,
    document: KnowledgeDocument,
    categories: list[KnowledgeCategory],
) -> SuggestionKind:
    """为单份文档生成建议；直接调 llm.chat，不 spawn。

    Returns:
        "suggested" 已写入建议；"unchanged" 模型认为当前分类正确（不落库）；
        "no_match" 模型明确回答现有分类都不合适（不落库，提示用户去建分类）；
        "skipped" 真的没拿到答案（正文读不到、调用失败、输出无法解析、code 不存在）。
    """
    try:
        excerpt = read_document_file(document.file_path, limit=_CLASSIFY_EXCERPT_CHARS)
    except (FileNotFoundError, OSError):
        logger.warning("分类建议跳过：正文读取失败 document_id=%s", document.id)
        return "skipped"

    result = await chat(
        # 便宜档：与 classifier 子 Agent（批量入口）保持同档，
        # 否则会出现"传一份文档和传两份文档用的模型不一样"这种没法解释的行为
        "chat-fast",
        [
            ChatMessage(role="system", content=_SINGLE_DOC_SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=(
                    f"候选分类：\n{_category_menu(categories)}\n\n"
                    f"文档标题：{document.title}\n"
                    f"文档正文（可能已截断）：\n{excerpt}"
                ),
            ),
        ],
        stream=False,
        db=db,
    )
    if result.finish_reason == "error":
        logger.warning("分类建议跳过：模型调用失败 document_id=%s", document.id)
        return "skipped"

    parsed = _parse_single_suggestion(result.content)
    if parsed is None:
        # 带上模型原样输出（截断）：不然"解析失败"这四个字什么线索都不给，
        # 既看不出是模型答非所问，还是它换了个 JSON 形状。
        # 这里是模型对分类问题的回答，不含文档正文。
        logger.warning(
            "分类建议跳过：模型输出无法解析 document_id=%s raw=%r",
            document.id,
            (result.content or "")[:200],
        )
        return "skipped"
    code, confidence, reason = parsed
    if code is None:
        # 模型认真回答了「现有分类都不合适」，这是结论不是故障
        logger.info("分类建议：现有分类无合适项 document_id=%s", document.id)
        return "no_match"

    by_code = {category.code: category for category in categories}
    target = by_code.get(code)
    if target is None:
        # 模型编了一个不存在的 code。不落库，避免管理页出现指向空分类的建议。
        logger.warning(
            "分类建议跳过：模型给出未知分类 code=%r document_id=%s 候选=%r",
            code,
            document.id,
            [category.code for category in categories],
        )
        return "skipped"

    if target.id == document.category_id:
        return "unchanged"

    await knowledge_document_crud.save_suggestion(
        db,
        document,
        suggested_category_id=target.id,
        confidence=confidence,
        reason=reason,
    )
    return "suggested"


async def _suggest_batch(
    db: AsyncSession,
    documents: list[KnowledgeDocument],
    categories: list[KnowledgeCategory],
    *,
    actor_user_id: int,
) -> tuple[int, int, int]:
    """两份及以上走 classify_documents 编排工作流。

    Returns:
        (写入建议数, 维持原分类数, 无合适分类数)

    工作流需要一个真实 AgentSession（AgentRegistry 外键约束），这里临时建一个、
    跑完即删。建议已经落到文档行，子 Agent 的对话记录没有保留价值，留着只会
    污染用户的会话列表。
    """
    by_id = {document.id: document for document in documents}
    by_code = {category.code: category for category in categories}

    session = await agent_session_crud.create(
        db,
        {"user_id": actor_user_id, "title": "知识库分类作业", "status": "active"},
    )
    await db.commit()
    session_id = session.id

    try:
        outcome = await classify_documents(
            spawn_manager,
            session_id=session_id,
            documents=[
                ClassificationDocument(
                    document_id=document.id,
                    title=document.title,
                    file_path=document.file_path,
                    current_category=(
                        next(
                            (c.code for c in categories if c.id == document.category_id),
                            None,
                        )
                    ),
                )
                for document in documents
            ],
            allowed_categories=[category.code for category in categories],
        )
    finally:
        # 无论工作流成功与否都不留下临时会话；删除失败只记日志，
        # 不能因为清理问题把已经拿到的建议一起丢掉。
        try:
            await agent_session_crud.hard_delete(db, session_id)
            await db.commit()
        except Exception:
            logger.exception("临时分类会话清理失败 session_id=%s", session_id)
            await db.rollback()

    if outcome.workflow_failure is not None:
        logger.warning("批量分类工作流失败：%s", outcome.workflow_failure)

    suggested = 0
    unchanged = 0
    no_match = 0
    for item in outcome.suggestions:
        document = by_id.get(item.document_id)
        if document is None:
            continue
        if not item.recommended_category.strip():
            # 与单份路径同样处理：模型明确说没有合适分类，是结论不是故障
            no_match += 1
            continue
        target = by_code.get(item.recommended_category)
        if target is None:
            continue
        if target.id == document.category_id:
            # 与单份路径同样处理：建议等于现分类就不落库，见 SuggestionOutcome.unchanged
            unchanged += 1
            continue
        await knowledge_document_crud.save_suggestion(
            db,
            document,
            suggested_category_id=target.id,
            confidence=item.confidence,
            reason=item.reason,
        )
        suggested += 1
    return suggested, unchanged, no_match


async def suggest_categories(
    db: AsyncSession,
    document_ids: list[int],
    *,
    actor_user_id: int,
) -> SuggestionOutcome:
    """为指定文档生成 AI 分类建议并落库。

    单份走直接 LLM 调用，多份走编排工作流——分界线是架构文档的反模式红线：
    单文档分类是单次动作，不该创建子 Agent。

    Args:
        db: 数据库会话；本函数只 flush 建议，由调用方决定何时 commit
            （批量路径内部为临时会话的建删各自 commit 一次）。
        document_ids: 待建议的文档 ID。
        actor_user_id: 触发者，批量路径用它作为临时会话归属。

    Returns:
        写入建议的份数与被跳过的份数。
    """
    documents = await knowledge_document_crud.list_by_ids(db, document_ids)
    if not documents:
        return SuggestionOutcome(suggested=0, skipped=0)

    # 「未分类」是收纳桶不是业务分类，必须排除出候选：把它摆进候选清单等于允许
    # 模型回答"保持原地不动"，而绝大多数待归类文档本来就在未分类里——那种建议
    # 落库后看着可点，点下去分类却纹丝不动，只是把建议清空了。
    categories = [
        category
        for category in await knowledge_category_crud.list_all(db)
        if category.code != UNCATEGORIZED_CODE
    ]
    if not categories:
        # 一个可用分类都没有时无从建议，直接返回而不是让模型对着空清单瞎猜。
        return SuggestionOutcome(suggested=0, skipped=len(documents))

    if len(documents) == 1:
        kind = await _suggest_one(db, documents[0], categories)
        return SuggestionOutcome(
            suggested=int(kind == "suggested"),
            skipped=int(kind == "skipped"),
            unchanged=int(kind == "unchanged"),
            no_match=int(kind == "no_match"),
        )

    suggested, unchanged, no_match = await _suggest_batch(
        db, documents, categories, actor_user_id=actor_user_id
    )
    return SuggestionOutcome(
        suggested=suggested,
        skipped=len(documents) - suggested - unchanged - no_match,
        unchanged=unchanged,
        no_match=no_match,
    )
