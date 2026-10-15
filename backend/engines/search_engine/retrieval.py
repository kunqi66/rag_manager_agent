"""检索编排：向量化 → 混合检索 → 取余弦分 → 受限探测 → 后置兜底。

本文件把 be-05 §5.2 的 ①③④⑤⑥ 串起来，产出「可放进 Prompt 的切片」+
「引用列表」+「受限计数」。**不做 Prompt、不调 LLM、不落库** —— 那些在 `qa.py` / `session.py`。

⚠️ **每问固定 3 次 Milvus 调用**（混合 / 稠密取分 / 受限探测）。这是刻意的取舍：
   取余弦分与受限探测都各自多花一次往返，换来的是「分数口径正确」与
   「权限缺失提示有数据」。相对一次 LLM 调用，这三次往返的耗时可以忽略。

🔴 **本模块不 import FastAPI**（分层铁律），也不直接读 Redis 的键名 ——
   探测缓存的键定义在 `clients/redis_client.py` 一侧由本模块拼装，
   拼装函数集中在 `_probe_cache_key`，避免键名散落。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from backend.clients import embedding_client, milvus_client, redis_client
from backend.core.settings import settings
from backend.engines.import_engine import list_visible_document_ids
from backend.engines.search_engine.acl import build_acl_expr, post_verify
from backend.schemas.auth import AuthContext
from backend.schemas.query import CitationItem

logger = logging.getLogger(__name__)

__all__ = ["RetrievalResult", "embed_query", "retrieve"]

#: 检索要 Milvus 回传的字段。
#: 🔴 **不含 title** —— Milvus 的 schema 里根本没有这一列（字典 §3），
#:    标题必须回 MySQL 权威表取，且只有复核通过的文档才取（红线 4）。
_OUTPUT_FIELDS = ["id", "document_id", "content", "heading_path"]


@dataclass(slots=True)
class RetrievalResult:
    """一次检索的完整产物。"""

    #: 可放进 Prompt 的切片（**已过后置复核**，按融合排名降序）
    chunks: list[dict] = field(default_factory=list)
    #: 引用列表（与 `chunks` 同源，供 SSE `citations` 与 `qa_message.citations`）
    citations: list[CitationItem] = field(default_factory=list)
    #: 受限文档数。**只给数量**（红线 4）。
    blocked_count: int = 0
    #: 受限文档标题。**默认空** —— 仅当 `acl_reveal_blocked_titles` 打开时才有值。
    blocked_titles: list[str] | None = None
    #: 融合后放行候选的 **chunk** id（`qa_log.recalled_ids`）
    recalled_ids: list[str] = field(default_factory=list)
    #: 后置复核通过的 **chunk** id（`qa_log.allowed_ids`）
    allowed_ids: list[str] = field(default_factory=list)
    #: 受限的 **document** id（`qa_log.blocked_ids`）
    blocked_ids: list[str] = field(default_factory=list)
    #: 最高**余弦**相似度（与 `kb_min_score` 同量纲）。无结果时为 `None`。
    top_score: float | None = None


async def embed_query(query: str) -> list[float]:
    """把问题向量化。

    🔴 **加 BGE 的查询指令前缀**（`settings.embedding_query_prefix`）。
       文档片段侧**不加**（见 `embedding_client` 头部）。两边都加或都不加
       会让相似度整体偏移，且不报错。

    ⚠️ 返回的向量**加了前缀**，直接喂给 Milvus 的稠密路；
       若要给 FAQ 索引做比对，需先按 be-08 的约定做 L2 归一化 ——
       两种口径别混（be-05 §5.2-①）。
    """
    vectors = await embedding_client.embed_texts(
        [f"{settings.embedding_query_prefix}{query}"]
    )
    if not vectors:
        # `embed_texts` 对非空入参恒返回同长度结果，走到这里说明依赖返回异常，
        # 而不是「没有结果」—— 必须显式失败，否则下游会拿 None 当向量。
        from backend.core.exceptions import DependencyError

        raise DependencyError("向量服务未返回问题向量")
    return vectors[0]


def _probe_cache_key(user_id: str, query: str) -> str:
    """受限探测的缓存键（字典 §4）。

    形态 `rag:query:probe:{user_id}:{query_hash}` —— 本函数是**唯一**拼装处。
    `query_hash` 用 sha256 前 16 位：sha256 保证不同问题不撞，
    截断只是为了让键短一点（碰撞概率在这个量级下可忽略）。

    ⚠️ 键里带 `user_id`：不同用户的受限集合不同，共享缓存会串权限。
    """
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    return f"rag:query:probe:{user_id}:{digest}"


async def _probe_blocked_document_ids(
    query: str,
    query_dense: list[float],
    user_id: str,
    visible_document_ids: set[str],
    limit: int,
) -> set[str]:
    """受限探测：**相同检索条件但不限 ACL**，返回被权限挡掉的 document_id 集合。

    实现要点（be-05 §5.2-⑤）：

    1. **`limit` 与放行侧同口径** —— 两侧候选空间不一致时差集没有意义。
    2. **保留 `document_id in [...]`（可见性）过滤，只摘掉 `acl_*` 段** ——
       可见性是「文档是否启用/是否软删」的**状态**过滤，不是权限过滤。
       连它一起摘掉的话，已停用、已删除的文档会被算成「因权限受限」，
       用户看到的是「有 N 篇你无权查看」，而实际是「有 N 篇已经下线了」——
       既误导用户，也污染 `qa_log.blocked_ids`（be-09 的缺口判定会读它）。
    3. **走混合检索**（与放行侧同一条路径），保证两次的候选集可比。
    """
    key = _probe_cache_key(user_id, query)
    redis = redis_client.get_client()
    try:
        cached = await redis_client.guard("GET", redis.get(key))
        if cached:
            payload = json.loads(cached)
            if isinstance(payload, list):
                return {item for item in payload if isinstance(item, str)}
    except Exception:  # noqa: BLE001 —— 缓存故障不该让问答失败
        # ⚠️ 与 `redis_client` 的「fail-closed」原则**不冲突**：
        #    那条铁律针对的是「拿不到 Redis 就无法防爆破/校验 refresh」这类安全机制。
        #    探测缓存只是省一次 Milvus 调用，miss 的后果是**多查一次**，不是放行。
        logger.warning("受限探测缓存读取失败，本次直连向量库", exc_info=True)

    # 可见性过滤（不含 acl_*）
    visible = sorted(visible_document_ids)
    probe_expr = f"document_id in {json.dumps(visible)}"

    hits = await milvus_client.hybrid_search(
        query_text=query,
        query_dense=query_dense,
        expr=probe_expr,
        limit=limit,
        output_fields=["id", "document_id"],
    )
    probe_ids = {
        hit["document_id"]
        for hit in hits
        if isinstance(hit.get("document_id"), str)
    }

    try:
        await redis_client.guard(
            "SETEX", redis.setex(key, settings.qa_probe_cache_seconds, json.dumps(sorted(probe_ids)))
        )
    except Exception:  # noqa: BLE001 —— 同上，写缓存失败不影响本次结果
        logger.warning("受限探测缓存写入失败", exc_info=True)

    return probe_ids


async def retrieve(
    db: AsyncSession,
    query: str,
    ctx: AuthContext,
    top_k: int,
) -> RetrievalResult:
    """完整检索链路。**本函数不抛「没检索到」这类异常** —— 空结果是正常业务态。

    Args:
        top_k: 用户请求的召回条数。内部会按 `retrieval_prefetch_ratio` 放大。
    """
    # ---- ⑧-前置：先把「可见文档集合」查出来（拼表达式与后置复核都要用）----
    # 只查一次，两处复用 —— 查两次的话，两次之间文档被停用会造成
    # 「表达式允许但复核拒绝」的**假**不一致告警，把真实告警淹没。
    visible_document_ids = await list_visible_document_ids(db)
    if not visible_document_ids:
        # 空知识库。短路掉 3 次 Milvus 调用 + 1 次 BGE 调用 ——
        # 更重要的是：`build_acl_expr` 对空集合会直接抛错（Milvus 的 `in []` 行为不可依赖）。
        logger.info("知识库中没有可检索文档，跳过检索")
        return RetrievalResult()

    # ---- ① 问题向量化（只算一次，三处复用）----
    query_dense = await embed_query(query)

    limit = max(1, top_k) * max(1, settings.retrieval_prefetch_ratio)
    acl = build_acl_expr(ctx, visible_document_ids)
    logger.debug(
        "检索 ACL：depts=%d roles=%d users=%d docs=%d",
        acl.dept_count,
        acl.role_count,
        acl.user_count,
        acl.doc_count,
    )

    # ---- ③ 前置过滤 + 混合检索 ----
    hits = await milvus_client.hybrid_search(
        query_text=query,
        query_dense=query_dense,
        expr=acl.expr,
        limit=limit,
        output_fields=_OUTPUT_FIELDS,
    )
    if not hits:
        return RetrievalResult()

    recalled_ids = [h["id"] for h in hits if isinstance(h.get("id"), str)]

    # ---- ④ 取余弦分（RRF 分不能跟 kb_min_score 比）----
    cosine_by_chunk = await milvus_client.dense_search(
        query_dense=query_dense, expr=acl.expr, limit=limit
    )

    # ---- ⑤ 受限探测（差集 = 因权限被挡掉的文档）----
    probe_document_ids = await _probe_blocked_document_ids(
        query=query,
        query_dense=query_dense,
        user_id=ctx.user_id,
        visible_document_ids=visible_document_ids,
        limit=limit,
    )

    # ---- ⑥ 后置兜底：MySQL 权威权限复核 ----
    candidate_document_ids = {
        h["document_id"] for h in hits if isinstance(h.get("document_id"), str)
    }
    verified = await post_verify(db, candidate_document_ids, ctx, visible_document_ids)

    # 受限 = 探测差集（放行侧没出现、但无 ACL 时能检到的）
    #      ∪ 后置兜底剔除的（Milvus 放行了但权威权限不认）
    blocked_document_ids = (probe_document_ids - verified.allowed) | verified.blocked

    # ---- 组装：只保留复核通过的切片，按融合排名降序 ----
    allowed_chunks: list[dict] = []
    citations: list[CitationItem] = []
    for hit in hits:
        document_id = hit.get("document_id")
        if not isinstance(document_id, str) or document_id not in verified.allowed:
            continue
        chunk_id = hit.get("id")
        if not isinstance(chunk_id, str):
            continue
        heading_path = hit.get("heading_path") or ""
        content = hit.get("content") or ""
        allowed_chunks.append(
            {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "title": verified.titles.get(document_id, document_id),
                "heading_path": heading_path,
                "content": content,
                "score": cosine_by_chunk.get(chunk_id, 0.0),
            }
        )
        citations.append(
            CitationItem(
                document_id=document_id,
                title=verified.titles.get(document_id, document_id),
                chunk_id=chunk_id,
                heading_path=heading_path,
                # 余弦口径；缺值时给 0.0 而不是 RRF 分 —— 混口径比缺值更坏
                score=round(cosine_by_chunk.get(chunk_id, 0.0), 4),
            )
        )

    # 截到用户要的条数。**注意**：这里截断的是「复核通过的」，
    # 而不是「融合后的前 top_k」—— 后者会在有多条被剔除时给出不足 top_k 的结果。
    allowed_chunks = allowed_chunks[: max(1, top_k)]
    citations = citations[: max(1, top_k)]

    top_score = max((c["score"] for c in allowed_chunks), default=None)

    blocked_titles: list[str] | None = None
    if settings.acl_reveal_blocked_titles and blocked_document_ids:
        # 🔴 默认关闭（红线 4）。打开会泄漏「存在某份敏感文档」这一事实。
        #    仅在演示场景临时打开 —— 见 settings 里该项的注释。
        blocked_titles = sorted(verified.titles.get(d, d) for d in blocked_document_ids)

    return RetrievalResult(
        chunks=allowed_chunks,
        citations=citations,
        blocked_count=len(blocked_document_ids),
        blocked_titles=blocked_titles,
        recalled_ids=recalled_ids,
        allowed_ids=[c["chunk_id"] for c in allowed_chunks],
        blocked_ids=sorted(blocked_document_ids),
        top_score=top_score,
    )
