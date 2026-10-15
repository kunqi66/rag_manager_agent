"""Milvus 客户端封装（pymilvus 3.0，collection `kb_chunk`）。

本模块负责**建 collection**（be-04 的职责）+ 写入 / 删除 / ACL 刷新。
be-05 只消费、**不得改 schema**（be-05 §4.2 第 7 条）。

两条硬约束：

1. 🔴 **`pymilvus` 是同步库** —— 所有调用必须经 `asyncio.to_thread` 进线程池。
   直接在协程里调会阻塞整个事件循环：症状不是报错，而是**所有**并发请求一起变慢，
   且 SQL/Redis 的耗时看起来毫无异常，极难归因。

2. 🔴 **Milvus 的 `filter` 表达式不支持绑定参数** —— 只能拼字符串。
   所以拼接前必须过 `_assert_safe_id` 白名单。这与「禁用 f-string 拼 SQL」不冲突：
   那条铁律针对的是 SQL，而这里没有别的选择；白名单是等效的防线。
   本模块的 id 全部由后端生成（`前缀_12位hex`），正常绝不会触发校验失败 ——
   它挡的是「将来某个调用方把用户输入当 document_id 传进来」。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any, TypeVar

from pymilvus import (
    AnnSearchRequest,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    RRFRanker,
)
from pymilvus.exceptions import MilvusException
from pymilvus.orm.schema import CollectionSchema

from backend.core.exceptions import DependencyError
from backend.core.settings import settings

logger = logging.getLogger(__name__)

__all__ = [
    "ensure_collection",
    "upsert_chunks",
    "delete_by_document",
    "refresh_document_acl",
    "close_client",
    "collection_ready",
    "hybrid_search",
    "dense_search",
]

T = TypeVar("T")

_client: MilvusClient | None = None

#: id 白名单。见模块头部第 2 条。
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

#: Milvus 侧 `content` 的字符上限（VARCHAR(max_length)）。
#: MySQL 的 `kb_chunk.content` 是 TEXT、**保留全文**；这里截断的只是**检索副本**。
#: 会超的只有「超长代码块」那一种（PRD 步骤 4 要求整体保留不切，见 chunking.py）。
#: 截断优于整批写入失败：BM25 命中前 60000 字符对该片段已足够定位，
#: 而一次写入失败会让整份文档导入失败。
_CONTENT_MAX_CHARS = 60000

#: 读回切片时的迭代批大小。**不是** limit，见 `_iter_document_rows`。
_QUERY_BATCH = 1000

#: 单个文档读回行数的**熔断上限**。真到这一步说明迭代器行为异常
#: （正常情况下一个文档不可能有 50 万切片），宁可报错也不静默少刷 ACL。
_MAX_CHUNKS_PER_DOC = 500_000

#: 刷新 ACL 时需要读回的字段。
#: 🔴 **必须包含 `content` 与 `dense`**：Milvus 的 `upsert` 是「整实体替换」，
#:    只写 ACL 四个字段会把正文与向量**清空** —— 表现为「改完权限后这份文档
#:    就再也检索不到了」，而且不报错。
_EXISTING_FIELDS = ["id", "document_id", "content", "heading_path", "dense"]


def _assert_safe_id(value: str, label: str) -> str:
    """拼接进 Milvus 表达式之前必须过这一关。"""
    if not isinstance(value, str) or not _SAFE_ID_RE.match(value):
        raise DependencyError(f"{label} 含非法字符，拒绝拼接 Milvus 表达式")
    return value


def get_client() -> MilvusClient:
    """进程内单例。pymilvus 的 MilvusClient 自带连接管理。"""
    global _client
    if _client is None:
        _client = MilvusClient(
            uri=f"http://{settings.milvus_host}:{settings.milvus_port}",
            # 建连接不在这里做：Milvus 没起时 `MilvusClient(...)` 也应能构造成功，
            # 否则 API 进程会因为「Milvus 暂时不可用」而整体起不来。
        )
    return _client


def close_client() -> None:
    """应用关闭时释放连接。"""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # noqa: BLE001 —— 关闭失败不该影响退出流程
            logger.warning("Milvus 连接关闭失败", exc_info=True)
        _client = None


async def _call(op: str, func: Callable[[], T]) -> T:
    """把同步调用挪进线程池，并把 `MilvusException` 收敛成 `DependencyError`。"""
    try:
        return await asyncio.to_thread(func)
    except MilvusException as exc:
        logger.error("Milvus 操作失败：%s", op)
        raise DependencyError(f"向量库不可用（{op}）") from exc
    except DependencyError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 连接层异常不一定是 MilvusException
        logger.error("Milvus 操作异常：%s", op, exc_info=True)
        raise DependencyError(f"向量库不可用（{op}）") from exc


# ═══════════════════════════════════════════════════════════════
# 建表
# ═══════════════════════════════════════════════════════════════


def _build_schema() -> CollectionSchema:
    """按字典 §3 建 schema。

    🔴 `content` 的 `enable_analyzer=True` + `analyzer_params={"type": "chinese"}`
       是**必须**的：不加的话 BM25 只做精确分词匹配，中文长句几乎永远命不中，
       而且不会报错 —— 只是稀疏那一路的召回率悄悄掉到接近 0，
       融合后看起来「能用但不太准」（CLAUDE.md 检索要点已记）。
    """
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)

    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("document_id", DataType.VARCHAR, max_length=64)
    schema.add_field(
        "content",
        DataType.VARCHAR,
        max_length=65535,
        enable_analyzer=True,
        analyzer_params={"type": "chinese"},
    )
    schema.add_field("heading_path", DataType.VARCHAR, max_length=512)
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=settings.embedding_dim)
    # 稀疏向量由 BM25 Function 从 content 自动生成 —— 写入时**不要**自己填 sparse
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)

    schema.add_field("acl_global", DataType.BOOL)
    for name, capacity in (
        ("acl_departments", 64),
        ("acl_roles", 16),
        ("acl_users", 256),
    ):
        schema.add_field(
            name,
            DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=capacity,
            max_length=64,
        )

    schema.add_function(
        Function(
            name="content_bm25",
            function_type=FunctionType.BM25,
            input_field_names=["content"],
            output_field_names=["sparse"],
        )
    )
    return schema


def _build_index_params():
    """稠密 COSINE + 稀疏 BM25 两路索引（混合检索的前提）。"""
    index_params = MilvusClient.prepare_index_params()
    index_params.add_index(
        field_name="dense",
        index_type=settings.milvus_index_type,
        metric_type=settings.milvus_metric_type,
    )
    index_params.add_index(
        field_name="sparse",
        # 稀疏向量的索引类型与度量是固定的搭配，不跟着 milvus_index_type 走
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
    )
    return index_params


async def ensure_collection() -> None:
    """collection 不存在就建（幂等）。写入前调用。"""

    def _sync() -> None:
        client = get_client()
        if client.has_collection(settings.milvus_collection):
            return
        client.create_collection(
            collection_name=settings.milvus_collection,
            schema=_build_schema(),
            index_params=_build_index_params(),
        )
        logger.info("已创建 Milvus collection：%s", settings.milvus_collection)

    await _call("ensure_collection", _sync)


# ═══════════════════════════════════════════════════════════════
# 写入
# ═══════════════════════════════════════════════════════════════


def _prepare_rows(rows: list[dict[str, Any]], acl: dict[str, Any]) -> list[dict[str, Any]]:
    """补齐 ACL 四字段并裁掉超长正文。

    调用方只给 `id / document_id / content / heading_path / dense`，
    四个 `acl_*` 一律在这里统一补 —— 让「ACL 从哪来」只有一个入口，
    防止某条路径忘了带 ACL 而导致该片**对所有非管理员不可见**（静默漏召）。
    """
    prepared: list[dict[str, Any]] = []
    for row in rows:
        content = row.get("content") or ""
        if len(content) > _CONTENT_MAX_CHARS:
            logger.warning(
                "切片 %s 正文 %d 字符，超过 Milvus 上限，检索副本已截断（MySQL 保留全文）",
                row.get("id"),
                len(content),
            )
            content = content[:_CONTENT_MAX_CHARS]
        prepared.append({**row, "content": content, **acl})
    return prepared


async def upsert_chunks(rows: list[dict[str, Any]], acl: dict[str, Any]) -> int:
    """批量 upsert 切片。

    **用 `upsert` 而不是 `insert`**（spec §5.5）：同一批 id 重复写不会产生重复行，
    这是「重试不先删」的前提 —— 先删会在第二次失败时把第一次的成果也丢掉。
    """
    if not rows:
        return 0
    prepared = _prepare_rows(rows, acl)
    await ensure_collection()

    def _sync() -> int:
        result = get_client().upsert(
            collection_name=settings.milvus_collection, data=prepared
        )
        return int(result.get("upsert_count", len(prepared)))

    return await _call("upsert_chunks", _sync)


async def delete_by_document(document_id: str) -> int:
    """删除某文档的全部切片（软删除文档 / 重建切片时调用）。

    按 `document_id` 删而不是按 id 列表删：调用方不必先查出全部 chunk id，
    也就不存在「查出来的列表被截断导致删不干净」这个隐患。
    """
    safe = _assert_safe_id(document_id, "document_id")

    def _sync() -> int:
        client = get_client()
        if not client.has_collection(settings.milvus_collection):
            return 0
        result = client.delete(
            collection_name=settings.milvus_collection,
            filter=f'document_id == "{safe}"',
        )
        return int(result.get("delete_count", 0))

    return await _call("delete_by_document", _sync)


def _iter_document_rows(client: MilvusClient, safe_document_id: str) -> list[dict[str, Any]]:
    """读回一个文档的**全部**切片行。

    🔴 **为什么不能用 `query(limit=...)` 读全量（2026-10-15 踩到，本函数的前身
       100% 失败，且失败得很安静）**：

       Milvus 的 `query` 结果窗口是 `offset + limit <= 16384`。超过**不是**
       「静默截断」，而是直接抛

           MilvusException: invalid max query result window,
           (offset+limit) should be in range [1, 16384], but got 100000

       本函数的前身把 limit 写成 `100_000`，注释里的理由是「默认 16384 会静默截断，
       所以显式调大」—— **推理方向反了**：16384 不是「默认值」而是**上限**，
       调大不会拿到更多，只会把「少刷一部分」变成「一条都刷不了」。
       于是 `refresh_document_acl` 对**每一个**文档都失败。

       **为什么这个失败特别隐蔽**：调用方按 spec §5.6 把它的异常吞掉
       （权限同步允许最终一致），所以接口照回 200、MySQL 里的权限**确实改了**，
       只有 Milvus 的 `acl_*` 留在旧值 —— 表现是「授权后搜不到 / 撤权后还能搜」。
       唯一能看见它的地方是日志里的 `向量库不可用（refresh_document_acl）`。

    → 读全量只能用 `query_iterator`（内部按 `batch_size` 翻页，不受该窗口限制）。
      它自己管翻页，所以这里**不再需要 offset/limit 的任何算术** ——
      凡是手写 `offset + limit` 的地方，都会再次撞上同一个上限。
    """
    rows: list[dict[str, Any]] = []
    iterator = client.query_iterator(
        collection_name=settings.milvus_collection,
        filter=f'document_id == "{safe_document_id}"',
        output_fields=_EXISTING_FIELDS,
        batch_size=_QUERY_BATCH,
    )
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            rows.extend(dict(item) for item in batch)
            if len(rows) > _MAX_CHUNKS_PER_DOC:
                # 不静默截断：少刷 ACL 会留下越权（旧 `acl_global=true` 的切片），
                # 宁可让调用方看到失败 —— 它在日志里，而越权不在任何地方可见。
                raise DependencyError(
                    f"文档 {safe_document_id} 切片数超过熔断上限 "
                    f"{_MAX_CHUNKS_PER_DOC}，拒绝半量刷新 ACL"
                )
    finally:
        iterator.close()
    return rows


async def refresh_document_acl(document_id: str, acl: dict[str, Any]) -> int:
    """权限变更后刷新该文档全部切片的四个 `acl_*` 字段。

    实现是「**查回 → 改 ACL → 整批 upsert**」，而不是「只 upsert ACL 字段」。
    原因见 `_EXISTING_FIELDS` 的注释：Milvus 的 upsert 是整实体替换，
    只带部分字段会把其余字段清空。

    代价是读回一次 768 维向量（`dense`），但权限变更**低频**，
    且远比重跑一遍 BGE 便宜（那要再调一次远端 GPU）。

    ⚠️ 本函数的失败**不向上抛给权限接口** —— 由调用方决定（spec §5.6：
       同步失败可接受最终一致，前提是 be-05 的后置兜底一定在）。
    """
    safe = _assert_safe_id(document_id, "document_id")

    def _sync() -> int:
        client = get_client()
        if not client.has_collection(settings.milvus_collection):
            return 0
        existing = _iter_document_rows(client, safe)
        if not existing:
            return 0
        # query 会把 dense 以 list[float] 返回，可直接回写
        rows = [{**item, **acl} for item in existing]
        result = client.upsert(
            collection_name=settings.milvus_collection, data=rows
        )
        return int(result.get("upsert_count", len(rows)))

    return await _call("refresh_document_acl", _sync)


# ═══════════════════════════════════════════════════════════════
# 检索（be-05 消费；本模块只提供原语，不做编排）
# ═══════════════════════════════════════════════════════════════

#: 稠密路的检索参数。`ef` 是 HNSW/autoindex 的「搜索宽度」：
#: 调大提升召回、变慢。64 是文档量在万级以下时的稳妥值。
_DENSE_PARAMS: dict[str, Any] = {
    "metric_type": settings.milvus_metric_type,
    "params": {"ef": 64},
}

#: 稀疏路（BM25）。度量固定 `BM25`，与建索引时一致，不跟 `milvus_index_type` 走。
_SPARSE_PARAMS: dict[str, Any] = {"metric_type": "BM25"}


async def collection_ready() -> bool:
    """collection 是否已存在。

    be-05 用它做短路：空知识库（或 Milvus 尚未建表）时直接返回空结果，
    而不是让 `search` 抛一个「collection not found」再被误报成 4001 依赖故障。
    """
    def _sync() -> bool:
        return bool(get_client().has_collection(settings.milvus_collection))

    return await _call("collection_ready", _sync)


def _flatten(hits: Any) -> list[dict[str, Any]]:
    """把 MilvusClient 的 `[[hit, ...]]` 摊平成 `[hit, ...]`。

    单条 query 恒返回「一个 query 对应一个内层列表」，但我们只发一条 query，
    所以取第 0 层。**保留这层适配**：Milvus 的返回形状在 Client 与 ORM 两套 API 下
    不同，写死 `result[0]` 在换 API 时会静默拿到空列表（而不是报错）。
    """
    if not isinstance(hits, list) or not hits:
        return []
    first = hits[0]
    return list(first) if isinstance(first, list) else []


def _entity_of(hit: dict[str, Any]) -> dict[str, Any]:
    """取出 hit 的实体字段。

    MilvusClient 的返回把 `output_fields` 放在 `entity` 下，但**主键 `id` 在顶层**。
    只读 `entity` 会拿不到 `id`；只读顶层会拿不到 `content`。
    """
    entity = hit.get("entity")
    return dict(entity) if isinstance(entity, dict) else {}


async def hybrid_search(
    query_text: str,
    query_dense: list[float],
    expr: str,
    limit: int,
    output_fields: list[str],
) -> list[dict[str, Any]]:
    """稠密 + 稀疏（BM25）混合检索，RRF 融合（PRD §6.2.2 / 字典 §3）。

    🔴 **两个子检索的 `limit` 都要放大**，由调用方传 `top_k * retrieval_prefetch_ratio`。
       因为 RRF 按**各路排名**融合、不是按分数加权：子检索只召回 `top_k` 条时
       两路交集少，去重后往往不足 `top_k`（极端情况只剩 1–2 条），
       表现为「问什么都只给一两条」，不报错。

    🔴 **稀疏路的 `data` 传的是原始查询文本**，不是向量 —— 该字段由 BM25 Function
       在服务端从文本生成。传向量会直接报 schema 不匹配。

    Returns:
        融合后的命中列表，`distance` 是 **RRF 分数**（量级约 `1/(k+rank)`，远小于 1）。
        ⚠️ **不要拿它跟 `kb_min_score` 比** —— 那是余弦口径。取余弦见 `dense_search`。
    """
    def _sync() -> list[dict[str, Any]]:
        client = get_client()
        if not client.has_collection(settings.milvus_collection):
            return []
        reqs = [
            AnnSearchRequest(
                data=[query_dense],
                anns_field="dense",
                param=_DENSE_PARAMS,
                limit=limit,
                expr=expr,
            ),
            AnnSearchRequest(
                data=[query_text],
                anns_field="sparse",
                param=_SPARSE_PARAMS,
                limit=limit,
                expr=expr,
            ),
        ]
        hits = client.hybrid_search(
            collection_name=settings.milvus_collection,
            reqs=reqs,
            # k=60 是 RRF 的标准取值（PRD §6.2.2 已锁定）。
            # k 越小，排名靠前的越占优；60 是「平滑但不迟钝」的通用默认。
            ranker=RRFRanker(60),
            limit=limit,
            output_fields=output_fields,
        )
        out: list[dict[str, Any]] = []
        for hit in _flatten(hits):
            entity = _entity_of(hit)
            # 主键在顶层，其余在 entity —— 合并成一层，调用方不必知道这个区别
            out.append(
                {
                    "id": entity.get("id", hit.get("id")),
                    "score": hit.get("distance"),
                    **entity,
                }
            )
        return out

    return await _call("hybrid_search", _sync)


async def dense_search(
    query_dense: list[float],
    expr: str,
    limit: int,
) -> dict[str, float]:
    """只走稠密路的检索，返回 `{chunk_id: 余弦相似度}`。

    存在的唯一理由：混合检索返回的 `distance` 是 **RRF 分数**，
    量纲与 `settings.kb_min_score`（按余弦设的 0.4）完全不同，
    直接比较必然判错 —— 要么全都「不达标」，要么全都「达标」。

    所以 `top_score` 与缺口判定要的余弦值必须**单独取一次**。
    代价是多一次 Milvus 调用（每问共 3 次：混合 + 本次 + 受限探测），
    相对一次 LLM 调用可忽略。
    """
    def _sync() -> dict[str, float]:
        client = get_client()
        if not client.has_collection(settings.milvus_collection):
            return {}
        hits = client.search(
            collection_name=settings.milvus_collection,
            data=[query_dense],
            anns_field="dense",
            search_params=_DENSE_PARAMS,
            filter=expr,
            limit=limit,
            output_fields=["id"],
        )
        scores: dict[str, float] = {}
        for hit in _flatten(hits):
            entity = _entity_of(hit)
            chunk_id = entity.get("id", hit.get("id"))
            distance = hit.get("distance")
            if isinstance(chunk_id, str) and isinstance(distance, (int, float)):
                scores[chunk_id] = float(distance)
        return scores

    return await _call("dense_search", _sync)
