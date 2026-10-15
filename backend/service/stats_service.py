"""运营看板服务（be-07）。

分两侧，**方向不能反**：

- **聚合侧**（`aggregate_window` / `aggregate_range` / `upsert_buckets`）——
  由 `tasks/stats_tasks.py` 调，也由 `backfill()` 调。**同一段代码**，
  所以不存在「回填算得对、定时算得不对」。
- **查询侧**（`get_*`）—— 由 `api/stats_router.py` 调，**只读 `stats_*` 聚合表**。

🔴 **本模块的核心决策是「整桶重算 + 覆盖写」，不是「在旧值上累加」。**
   理由三条（be-07 §4.2），缺一条这个设计就不成立：

   1. `qa_log` 的落库时刻与提问时刻之间有事务提交 + 网络往返 + 连接池排队，
      会**跨桶迟到**。用 `+=` 累加时，迟到行会被计进**另一轮**的增量里 ——
      哪一轮加、加成多少取决于任务恰好几点跑，**不可复现**。
   2. 重算天然幂等：桶的值完全由 `created_at ∈ [桶起点, 桶终点)` 的行决定。
      这让**重试**与**回填**都不需要额外机制。
   3. 回填即同一段代码 —— 不需要写第二套「修正」逻辑。

🔴 **`stats_daily` 同样直连 `qa_log` 重算**，不是「把当天 24 个 hourly 加起来」：
   小时表有保留期会被清理，一旦被清理，从它派生日表就会**静默算少**。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, Select, and_, case, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import ForbiddenError, ParamError
from backend.core.ids import new_id
from backend.core.settings import settings
from backend.models.kb_chunk import KbChunk
from backend.models.kb_document import KbDocument
from backend.models.qa_log import QaLog
from backend.models.stats import StatsDaily, StatsHourly
from backend.models.sys_role import SysRole
from backend.models.sys_user import SysUserRole
from backend.schemas.auth import AuthContext
from backend.schemas.stats import (
    BucketRow,
    DashboardQuery,
    Granularity,
    LatencyHistogramDTO,
    OverviewDTO,
    TopKnowledgeItem,
    TopQuestionItem,
    TrendDTO,
    TrendPoint,
    TrendQuery,
)

logger = logging.getLogger(__name__)

__all__ = [
    "aggregate_range",
    "aggregate_window",
    "backfill",
    "get_latency_histogram",
    "get_overview",
    "get_top_knowledge",
    "get_top_questions",
    "get_trend",
    "resolve_window",
    "upsert_buckets",
]

#: 表名与模型：聚合侧按 grain 取用，查询侧同理。抽成映射避免每处写 if/else 分支。
_GRAIN_MODEL: dict[Granularity, type[StatsHourly] | type[StatsDaily]] = {
    "hour": StatsHourly,
    "day": StatsDaily,
}


# ═══════════════════════════════════════════════════════════════
# 时间窗
# ═══════════════════════════════════════════════════════════════


def _now() -> datetime:
    """当前时刻的 **naive** 表示（Asia/Shanghai 口径）。

    🔴 **整个模块只用 naive 时间**：`qa_log.created_at` 的 `server_default=func.now()`
       取的是 MySQL 服务端时间；Celery 已配 `enable_utc=False` + `timezone="Asia/Shanghai"`；
       前端 `resolveRange` 发的也是本地 naive 串。若在某一环引入 UTC 感知时间，
       桶边界会整体偏移 8 小时 —— 症状是「今天的数据跑到昨天去」，且**不报错**。

    ⚠️ 用 `datetime.now(UTC).replace(tzinfo=None)` 而不是 `datetime.now()`：
       后者取**操作系统本地时区**，在 UTC 的服务器上会直接算错 8 小时。
       项目约定是「naive 但语义上是 +08:00」，所以取 UTC 再手动加偏移最稳。
    """
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=8)


def floor_hour(moment: datetime) -> datetime:
    """整点下取整。"""
    return moment.replace(minute=0, second=0, microsecond=0)


def floor_day(moment: datetime) -> datetime:
    """当天 00:00:00。"""
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def resolve_window(
    start: datetime | None, end: datetime | None, *, granularity: Granularity = "day"
) -> tuple[datetime, datetime]:
    """把可选的时间参数补成**生效窗口**，并做全部校验。

    返回 `(start, end)` 都是 naive。校验失败抛 `ParamError`(1001) ——
    ⚠️ **不能用裸 `BizError(msg)`**：它的 code 默认是 `INTERNAL_ERROR`(5000)，
    前端按业务码分支时会当成服务端故障（CLAUDE.md 已知坑）。
    """
    now = _now()
    end = end or now
    start = start or (end - timedelta(days=settings.stats_default_range_days))

    if start > end:
        raise ParamError("起始时间不能晚于结束时间")

    span_days = (end - start).total_seconds() / 86400
    if span_days > settings.stats_query_max_days:
        raise ParamError(f"查询跨度不得超过 {settings.stats_query_max_days} 天")
    if granularity == "hour" and span_days > settings.stats_hour_range_max_days:
        # `stats_hourly` 有保留期，且「按小时看一年」是 8760 个点，前端也画不动。
        raise ParamError(
            f"按小时查询只允许 {settings.stats_hour_range_max_days} 天以内的范围，"
            f"更长的范围请改用 granularity=day"
        )
    return start, end


# ═══════════════════════════════════════════════════════════════
# 延时直方图与 P95
# ═══════════════════════════════════════════════════════════════


def _bucket_index(edges: list[int], latency_ms: int) -> int:
    """`latency_ms` 落在第几个桶。

    🔴 **末桶是开区间** `[edges[-1], +∞)`。少了这个开区间，
       「超过 5 秒」的请求会**全部落空**，直方图总数 ≠ `pv`，
       而**没人会发现**（图看起来只是矮了一点）。
    """
    for i in range(len(edges) - 1):
        if latency_ms < edges[i + 1]:
            return i
    return len(edges) - 2


def _histogram(latencies: list[int], edges: list[int]) -> dict[str, int]:
    """把一组耗时落进桶，返回 `{桶下界字符串: 计数}`。

    键必须是**字符串** —— JSON 对象的键只能 是字符串，存 int 键会在序列化时
    被静默转成字符串，读回来时类型不一致。
    """
    counts = [0] * (len(edges) - 1)
    for value in latencies:
        counts[_bucket_index(edges, value)] += 1
    return {str(edges[i]): counts[i] for i in range(len(counts))}


def p95_from_histogram(buckets: dict[str, int] | None, edges: list[int] | None = None) -> int:
    """从直方图**插值估算** P95。

    🔴 **为什么是估算而不是精确分位数**：be-07 §4.1 原稿写
       `PERCENTILE(latency_ms, 0.95)` —— **MySQL 8 没有这个聚合函数**
       （那是 Oracle / PostgreSQL 的）。三种可行写法：① `PERCENT_RANK()` 窗口函数；
       ② 排序取第 `ceil(0.95*n)` 行；③ 用已有直方图插值。**取 ③**：
       直方图本来就要为 `latency` 端点算，P95 顺带得出，**不额外扫一遍 `qa_log`**。
       代价是精度受桶边界限制 ⇒ DTO 里如实标为估算，不说成精确分位数。
       （字典 §9 #117）

    插值规则：定位到 P95 所在的桶，在该桶区间内按累计比例线性插值。
    末桶没有上界，取 `edges[-1]` 作为估计值（**低估**，故注释里写明）。
    """
    if not buckets:
        return 0
    edges = edges or settings.stats_latency_edges
    counts = [int(buckets.get(str(e), 0) or 0) for e in edges[:-1]]
    total = sum(counts)
    if total == 0:
        return 0

    rank = 0.95 * total
    cumulative = 0
    for i, count in enumerate(counts):
        if cumulative + count >= rank:
            lower = edges[i]
            # 末桶无上界：返回下界即可（低估，不虚报）。
            if i == len(counts) - 1:
                return int(lower)
            upper = edges[i + 1]
            within = (rank - cumulative) / count if count else 0.0
            return int(lower + (upper - lower) * within)
        cumulative += count
    return int(edges[-2])


# ═══════════════════════════════════════════════════════════════
# 聚合侧
# ═══════════════════════════════════════════════════════════════


def _bucket_expr(grain: Granularity):
    """`created_at` → 桶标识的 SQL 表达式。

    ⚠️ `day` 用 `DATE(created_at)`（返回 `date`），`hour` 用
       `DATE_FORMAT(created_at, '%Y-%m-%d %H:00:00')`。

       **不用 `DATE_FORMAT` 给 day 用**（`%Y-%m-%d`）：那返回的是**字符串**，
       而 `stats_daily.bucket_date` 是 DATE 列，MySQL 会隐式转换 ——
       转换失败的形态是「插入 0000-00-00」或静默截断，取决于 sql_mode。
       直接给 `DATE()` 让类型在两边一致。
    """
    if grain == "day":
        return func.date(QaLog.created_at)
    return func.date_format(QaLog.created_at, "%Y-%m-%d %H:00:00")


def _no_hit_condition():
    """「未命中」的判据：**没有放行候选** 或 **最高相似度低于阈值**。

    ⚠️ `allowed_ids` 是 JSON 列，用 `JSON_LENGTH` 判空：
       `JSON_LENGTH(NULL)` 返回 NULL（不是 0），所以必须 `COALESCE(..., 0)` ——
       否则「allowed_ids 为 NULL」的行既不算命中也不算未命中，**两边都漏**，
       而总数看起来仍然自洽（少的那部分没人对账）。
    """
    return (
        and_(
            func.coalesce(func.json_length(QaLog.allowed_ids), 0) == 0,
        )
    ) | (func.coalesce(QaLog.top_score, 0) < settings.kb_min_score)


def _base_aggregate_select(grain: Granularity, start: datetime, end: datetime) -> Select:
    """一桶一行的标量指标聚合。**不含 TOP 榜**（那个要展开 JSON，另查）。"""
    bucket = _bucket_expr(grain).label("bucket")
    return (
        select(
            bucket,
            func.count().label("pv"),
            func.count(func.distinct(QaLog.user_id)).label("uv"),
            func.count(func.distinct(QaLog.session_id)).label("session_count"),
            func.sum(case((QaLog.faq_hit.is_(True), 1), else_=0)).label("faq_hit_count"),
            func.sum(case((_no_hit_condition(), 1), else_=0)).label("no_hit_count"),
            func.coalesce(func.sum(QaLog.token_in), 0).label("token_in_sum"),
            func.coalesce(func.sum(QaLog.token_out), 0).label("token_out_sum"),
            func.coalesce(func.avg(QaLog.latency_ms), 0).label("latency_avg_ms"),
            # `blocked_ids` 存 **document id**，但这里只是数个数，粒度不影响（字典 §9 #113）。
            func.coalesce(func.sum(func.json_length(QaLog.blocked_ids)), 0).label(
                "blocked_count"
            ),
        )
        .where(QaLog.created_at >= start, QaLog.created_at < end)
        .group_by(bucket)
    )


async def _rank_documents(
    db: AsyncSession, chunk_hits: dict[str, int], n: int
) -> list[dict[str, Any]]:
    """`{chunk_id: 命中次数}` → `[{document_id, cite_count}]`，取前 N。

    🔴 **必须换回 `document_id`**：`qa_log.allowed_ids` 存的是 **chunk id**，
       而榜要的是文档（字典 §9 #113）。直接把 chunk id 当 document id 用 →
       文档热度算错且**不报错**：每个切片都成了"一份独立文档"，
       榜上全是 `chk_xxx` 这种 id，标题一个都查不到，而接口照回 200。
    """
    if not chunk_hits:
        return []
    # chunk id → document id。一次 IN 查询走主键，比逐条查省 N-1 次往返。
    map_stmt = select(KbChunk.id, KbChunk.document_id).where(KbChunk.id.in_(list(chunk_hits)))
    chunk_to_doc = {r.id: r.document_id for r in (await db.execute(map_stmt)).all()}

    doc_hits: dict[str, int] = defaultdict(int)
    for chunk_id, hits in chunk_hits.items():
        document_id = chunk_to_doc.get(chunk_id)
        if document_id is None:
            # 切片已被删（文档重建/删除）而 `qa_log` 是历史日志 —— 正常现象，跳过。
            # ⚠️ **不要逐条 `logger.warning`**：一份文档被重建会产生几百条，
            #    告警风暴会淹没真正的异常。
            continue
        doc_hits[document_id] += hits

    ranked = sorted(doc_hits.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return [{"document_id": doc_id, "cite_count": hits} for doc_id, hits in ranked]


async def aggregate_range(
    db: AsyncSession, grain: Granularity, start: datetime, end: datetime
) -> list[BucketRow]:
    """把 `[start, end)` 内的 `qa_log` 按 `grain` 算成各桶指标。**只算不写。**

    这是**整桶重算**的实现：返回值完全由源数据决定，与库里已有的桶值无关 ——
    幂等性的根因就在这一句。

    分两趟查：

    1. **标量指标**下推给 MySQL（`SELECT ... GROUP BY 桶`）—— 这是重的那部分，
       让数据库做最省。
    2. **明细**（`question` / `latency_ms` / `allowed_ids`）流式取回，在 Python 里
       按桶分组算直方图与 TOP 榜。

    🔴 **第 2 趟必须按桶分组，不能算成「窗口级」再喂给每个桶。**
       那样每个桶都存着同一份窗口 TOP，而查询侧合并多桶时是**求和**
       （`get_top_questions`）—— 一个 3 小时的窗口会把计数**乘 3**，
       而数字看起来完全正常（只会觉得"大家问得挺多"）。
       守门用例：`verify_stats.py` T1-30（同一问题在相邻两桶各出现一次，
       合并后计数必须是 2，不是 4）。

    ⚠️ 用 `db.stream()` 逐行消费而不是 `.all()`：回填一年的日桶时，
       明细行数是**整年**的量级，全量驻留内存会把 worker 拖垮。
       流式的内存是 O(桶数 × TOP 上限)，与行数无关。
    """
    edges = settings.stats_latency_edges
    n = settings.stats_top_n

    scalar_rows = (await db.execute(_base_aggregate_select(grain, start, end))).all()
    if not scalar_rows:
        return []

    # 逐桶累积器。三种都按桶分——这是「不能算成窗口级」的落地。
    latencies: dict[datetime, list[int]] = defaultdict(list)
    question_hits: dict[datetime, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bucket_chunk_hits: dict[datetime, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    detail_stmt = select(
        QaLog.created_at, QaLog.question, QaLog.latency_ms, QaLog.allowed_ids
    ).where(QaLog.created_at >= start, QaLog.created_at < end)

    async for row in await db.stream(detail_stmt):
        bucket = _normalize_bucket(grain, row.created_at)
        latencies[bucket].append(int(row.latency_ms or 0))
        if row.question:
            question_hits[bucket][row.question] += 1
        for chunk_id in row.allowed_ids or []:
            bucket_chunk_hits[bucket][str(chunk_id)] += 1

    # chunk → document 的换算是**全局一次**（不在桶循环里）：同一份文档在多个桶
    # 被引用时，只需要查一次映射表。
    all_chunk_hits: dict[str, int] = defaultdict(int)
    for hits in bucket_chunk_hits.values():
        for chunk_id, count in hits.items():
            all_chunk_hits[chunk_id] += count
    chunk_to_doc = await _load_chunk_map(db, all_chunk_hits)

    rows: list[BucketRow] = []
    for row in scalar_rows:
        bucket = _normalize_bucket(grain, row.bucket)
        buckets = _histogram(latencies.get(bucket, []), edges)
        rows.append(
            BucketRow(
                bucket=bucket,
                pv=int(row.pv or 0),
                uv=int(row.uv or 0),
                session_count=int(row.session_count or 0),
                faq_hit_count=int(row.faq_hit_count or 0),
                no_hit_count=int(row.no_hit_count or 0),
                token_in_sum=int(row.token_in_sum or 0),
                token_out_sum=int(row.token_out_sum or 0),
                latency_avg_ms=int(row.latency_avg_ms or 0),
                latency_p95_ms=p95_from_histogram(buckets, edges),
                latency_buckets=buckets,
                blocked_count=int(row.blocked_count or 0),
                # ⚠️ **本桶**的榜，不是窗口的。同义不同字算两条 ——
                #    语义去重是 be-08 的 FAQ 聚类职责，聚合层做会把分钟级任务变成小时级。
                top_questions=_rank_questions(question_hits.get(bucket, {}), n),
                top_documents=_rank_documents(
                    bucket_chunk_hits.get(bucket, {}), chunk_to_doc, n
                ),
            )
        )
    return rows


async def _load_chunk_map(db: AsyncSession, chunk_hits: dict[str, int]) -> dict[str, str]:
    """`chunk_id → document_id` 映射表。一次 IN 查询。"""
    if not chunk_hits:
        return {}
    map_stmt = select(KbChunk.id, KbChunk.document_id).where(KbChunk.id.in_(list(chunk_hits)))
    return {r.id: r.document_id for r in (await db.execute(map_stmt)).all()}


def _rank_questions(hits: dict[str, int], n: int) -> list[dict[str, Any]]:
    ranked = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return [{"question": q, "count": c} for q, c in ranked]


def _rank_documents(
    chunk_hits: dict[str, int], chunk_to_doc: dict[str, str], n: int
) -> list[dict[str, Any]]:
    """`{chunk_id: 次数}` + 映射表 → `[{document_id, cite_count}]` 前 N。"""
    doc_hits: dict[str, int] = defaultdict(int)
    for chunk_id, hits in chunk_hits.items():
        document_id = chunk_to_doc.get(chunk_id)
        if document_id is None:
            # 切片已被删（文档重建/删除）而 `qa_log` 是历史日志 —— 正常现象，跳过。
            # ⚠️ **不要逐条 `logger.warning`**：一份文档被重建会产生几百条，
            #    告警风暴会淹没真正的异常。
            continue
        doc_hits[document_id] += hits
    return [
        {"document_id": doc_id, "cite_count": hits}
        for doc_id, hits in sorted(doc_hits.items(), key=lambda kv: kv[1], reverse=True)[:n]
    ]


def _normalize_bucket(grain: Granularity, value: Any) -> datetime:
    """把**桶键**统一成 `datetime`（已按 `grain` 下取整）。

    三种输入形态都要处理：

    - `day` 桶从 `GROUP BY DATE(...)` 回来是 `datetime.date`；
    - `hour` 桶从 `DATE_FORMAT(..., '%H:00:00')` 回来是 `str`；
    - **原始 `created_at`**（明细流式那一趟）是**带分秒的 `datetime`**。

    🔴 **第三种的取整是必须的，不是洁癖。** 明细趟算出的桶键要拿去和聚合趟的桶键
       **对字典**（`latencies[bucket]` / `question_hits[bucket]`）。若 `created_at`
       原样返回，`2026-10-14 09:37:00` 与聚合趟的 `2026-10-14 09:00:00` 就是两个键 ——
       查不到任何明细 ⇒ **直方图恒空、TOP 榜恒空**，而标量指标（pv / uv）全对。
       症状是「看板数字都对，就是图表没数据」，且两个趟各自都"没报错"。

    `BucketRow.bucket` 的 pydantic 校验也会拒 —— 但那是**兜底**，不是这里可以偷懒的理由：
    校验只保证类型是 `datetime`，保证不了「已取整」。
    """
    if isinstance(value, str):
        # 只可能来自 `DATE_FORMAT`，形态固定。
        moment = datetime.strptime(value, "%Y-%m-%d %H:00:00")
    elif isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    else:  # pragma: no cover —— 防御性分支，SQL 侧不该产出别的类型
        # ⚠️ 用 `TypeError` 而不是 `ParamError`：这不是客户端的参数错，
        #    是**服务端自己**的类型不一致（5000 INTERNAL_ERROR 才是诚实的答复）。
        #    报成 1001 会把排查方向指到调用方，误导下一次定位。
        raise TypeError(f"无法识别的桶键类型：{type(value)!r}")

    return floor_hour(moment) if grain == "hour" else floor_day(moment)


async def upsert_buckets(db: AsyncSession, grain: Granularity, rows: list[BucketRow]) -> int:
    """把整桶结果**覆盖写**进对应的表。返回影响行数。

    🔴 **用 `INSERT ... ON DUPLICATE KEY UPDATE` 一次定胜负**，
       不用「先 SELECT 判断存在再 INSERT/UPDATE」：
       后者是两次往返 + 一个竞态窗口 —— 手动回填撞上 beat 定时任务时，
       会有一个拿到 `IntegrityError`。**幂等性的保证必须在数据库里，
       不在应用代码的分支里。**
    """
    if not rows:
        return 0

    table = _GRAIN_MODEL[grain].__table__
    bucket_col = "bucket_start" if grain == "hour" else "bucket_date"
    now = _now()

    payload: list[dict[str, Any]] = []
    for row in rows:
        bucket_value = row.bucket if grain == "hour" else row.bucket.date()
        payload.append(
            {
                "id": new_id("sth" if grain == "hour" else "std"),
                bucket_col: bucket_value,
                "pv": row.pv,
                "uv": row.uv,
                "session_count": row.session_count,
                "faq_hit_count": row.faq_hit_count,
                "faq_hit_rate": row.faq_hit_rate,
                "no_hit_count": row.no_hit_count,
                "token_in_sum": row.token_in_sum,
                "token_out_sum": row.token_out_sum,
                "latency_avg_ms": row.latency_avg_ms,
                "latency_p95_ms": row.latency_p95_ms,
                "latency_buckets": row.latency_buckets,
                "blocked_count": row.blocked_count,
                "top_questions": row.top_questions,
                "top_documents": row.top_documents,
                "updated_at": now,
            }
        )

    stmt = mysql_insert(table).values(payload)
    # ⚠️ **`id` 与桶列不进 UPDATE 的 SET**：
    #    - `id` 是主键，冲突时改它等于换行，会把原本那行的 id 覆盖掉；
    #    - 桶列是冲突键本身，值必然相同，写进去只是无谓的写放大。
    #    - `updated_at` **要进**，它是「本桶最近一次重算时间」，也是静默失败的唯一
    #      可观测手段（字典 §9 #123）。
    update_cols = {
        col: stmt.inserted[col]
        for col in payload[0]
        if col not in {"id", bucket_col}
    }
    result = await db.execute(stmt.on_duplicate_key_update(**update_cols))
    await db.commit()
    return int(result.rowcount or 0)


async def aggregate_window(db: AsyncSession, start: datetime, end: datetime) -> dict[str, int]:
    """对 `[start, end)` 跑两种颗粒度并 UPSERT。**幂等**：同一区间跑 N 次结果相同。"""
    summary: dict[str, int] = {}
    for grain in ("hour", "day"):
        rows = await aggregate_range(db, grain, start, end)
        summary[grain] = await upsert_buckets(db, grain, rows)
    logger.info(
        "运营看板聚合完成：窗口 [%s, %s)，小时桶 %s 行、日桶 %s 行",
        start,
        end,
        summary.get("hour", 0),
        summary.get("day", 0),
    )
    return summary


async def backfill(db: AsyncSession, start: datetime, end: datetime) -> dict[str, int]:
    """按区间重跑聚合。**就是 `aggregate_window`** —— 刻意不做第二套逻辑。

    「回填」与「定时聚合」的区别**只是窗口参数不同**。写第二套「修正」逻辑的话，
    迟早出现「回填算得对、定时算得不对」（或反之），而两者的输出是同一批列，
    对不上时**没有任何报错**，只能靠人肉对账。
    """
    return await aggregate_window(db, start, end)


# ═══════════════════════════════════════════════════════════════
# 查询侧（只读聚合表）
# ═══════════════════════════════════════════════════════════════


def _pick_model(start: datetime, end: datetime, granularity: Granularity | None) -> Granularity:
    """选小时表还是日表。

    - `trend` 有显式 `granularity` → **用它**（不需要猜）。
    - `overview` / 榜单没有该参数 → 按**跨度**猜：≤ `stats_hour_table_max_hours`
      用小时表（更细），否则用日表（更省）。
    """
    if granularity is not None:
        return granularity
    hours = (end - start).total_seconds() / 3600
    return "hour" if hours <= settings.stats_hour_table_max_hours else "day"


def _window_select(grain: Granularity, start: datetime, end: datetime) -> Select:
    """按 grain 选出窗口内的聚合行。

    ⚠️ 桶列的**边界语义**与聚合侧一致：`[start, end)`（前闭后开）。
       `stats_hourly` 的桶是整点，所以「起点 10:30」会命中 10:00 那个桶 ——
       即**桶的整体值**，含 10:00–10:30 那段。这是刻意的：
       读聚合表就不可能按半点切。前端若要精确到分钟，得等下一轮聚合。
    """
    model = _GRAIN_MODEL[grain]
    if grain == "hour":
        return (
            select(model)
            .where(model.bucket_start >= start, model.bucket_start < end)
            .order_by(model.bucket_start)
        )
    # 🔴 日桶列是 `DATE`，必须把窗口先转成 `date` 再比。
    #    直接拿 `datetime` 去比 DATE 列，MySQL 会把两边都提升成本地时间字符串比较 ——
    #    `'2026-10-14' >= '2026-10-14 09:30:00'` 是 **False**（字符串逐位比，短的先结束），
    #    于是**当天那个桶会被静默漏掉**：症状是「今天的数据时有时无」，
    #    而一旦窗口起点恰好是 00:00:00，比较又变成 True，看起来像是「偶发」。
    return (
        select(model)
        .where(model.bucket_date >= start.date(), model.bucket_date <= end.date())
        .order_by(model.bucket_date)
    )
    # ⚠️ 日桶用**闭区间** `<= end.date()`（小时桶是前闭后开）：日桶是**原子**的，
    #    没法按半点切开。窗口终点落在今天中午时，仍要给出今天那一桶 ——
    #    否则用户切「近 7 天」会看不到今天的任何数据（而那正是他最想看的一天）。
    #    代价是今天那一桶含聚合时刻之后的数据（见 `_count_today` 的滞后说明）。


async def get_overview(
    db: AsyncSession, user: AuthContext, query: DashboardQuery
) -> OverviewDTO:
    """KPI 卡数据。`user` 目前只用于一致性（本端点是纯聚合，不涉数据权限）。"""
    del user  # 参数保留是为了与其它 `get_*` 同形，便于将来统一加数据权限
    start, end = resolve_window(query.start_time, query.end_time)
    grain = _pick_model(start, end, None)

    rows = list((await db.execute(_window_select(grain, start, end))).scalars())

    pv = sum(r.pv for r in rows)
    faq_hit_count = sum(r.faq_hit_count for r in rows)
    no_hit_count = sum(r.no_hit_count for r in rows)

    # 🔴 `uv` 跨桶**不能直接当人数**：各桶的 uv 是桶内去重，SUM 得到的是**人次**。
    #    但「人数」也求不出精确值（需要明细或 HyperLogLog，本期不做）。
    #    ⇒ 给「各桶之和」并用 `uv_is_exact` 告诉前端这个数能不能加「人」字。
    #    窗口恰好落在一个桶内时，那个桶的 uv 就是真人数。
    uv = sum(r.uv for r in rows)
    uv_is_exact = len(rows) == 1

    # 🔴 平均耗时**按 pv 加权**：Σ(桶avg × 桶pv) / Σ桶pv。
    #    各桶 avg 取算术平均是错的 —— 只问 1 次的桶与问 5000 次的桶等权，
    #    10 个桶就能把整体均值拉偏几倍，且**看起来完全正常**（字典 §9 #119）。
    weighted = sum(r.latency_avg_ms * r.pv for r in rows)
    avg_latency_ms = int(weighted / pv) if pv else 0

    merged_buckets: dict[str, int] = defaultdict(int)
    for row in rows:
        for key, count in (row.latency_buckets or {}).items():
            merged_buckets[str(key)] += int(count or 0)

    edges = settings.stats_latency_edges
    merged = {str(e): merged_buckets.get(str(e), 0) for e in edges[:-1]}

    updated_at = max((r.updated_at for r in rows), default=None)
    today_question_count = await _count_today(db)

    return OverviewDTO(
        start_time=start,
        end_time=end,
        updated_at=updated_at,
        granularity=grain,
        pv=pv,
        uv=uv,
        uv_is_exact=uv_is_exact,
        document_count=await _count_documents(db),
        today_question_count=today_question_count,
        faq_hit_count=faq_hit_count,
        faq_hit_rate=(faq_hit_count / pv) if pv else 0.0,
        blocked_count=sum(r.blocked_count for r in rows),
        no_hit_count=no_hit_count,
        # 覆盖率 = 1 - 未命中率（全局 #69：覆盖率由本模块算，缺口池由 be-09 作明细下钻）
        coverage_rate=(1 - no_hit_count / pv) if pv else 0.0,
        avg_latency_ms=avg_latency_ms,
        latency_p95_ms=p95_from_histogram(merged, edges),
    )


async def _count_documents(db: AsyncSession) -> int:
    """知识单元总数（全量，软删的除外）。

    ⚠️ 这是**唯一一处非 `stats_*` 的读**（be-07 §7 明确允许）。
       `kb_document` 是小表且 `deleted_at` 上有条件，代价可忽略。
    """
    stmt = select(func.count()).select_from(KbDocument).where(KbDocument.deleted_at.is_(None))
    return int(await db.scalar(stmt) or 0)


async def _count_today(db: AsyncSession) -> int:
    """今日提问量。

    🔴 **只读聚合表，不扫 `qa_log`**（PRD §8.5 的硬要求）。
       代价是**滞后**：当日 00:00 之后的数据若还没聚合，这里会滞后最多一个 beat 周期
       （10 分钟）。看板本来就标着「数据每 10 分钟自动聚合一次，非实时」，一致。

    ⚠️ 用「日桶」而不是「小时桶求和」：日界口径必须与 `stats_daily` 一致，
       否则会出现「日表说 218、小时表加起来 219」这种没人能解释的差 1。
    """
    today = floor_day(_now())
    stmt = select(func.coalesce(func.sum(StatsDaily.pv), 0)).where(
        StatsDaily.bucket_date == today.date()
    )
    return int(await db.scalar(stmt) or 0)


async def get_trend(db: AsyncSession, query: TrendQuery) -> TrendDTO:
    """趋势序列。**一次返回全部指标**（be-07 §3.1 修订表第 2 条）。"""
    start, end = resolve_window(query.start_time, query.end_time, granularity=query.granularity)
    grain: Granularity = query.granularity

    rows = list((await db.execute(_window_select(grain, start, end))).scalars())

    points: list[TrendPoint] = []
    for row in rows:
        bucket = row.bucket_start if grain == "hour" else row.bucket_date
        points.append(
            TrendPoint(
                bucket=format_bucket(grain, bucket),
                token_in=row.token_in_sum,
                token_out=row.token_out_sum,
                question_count=row.pv,
                avg_latency_ms=row.latency_avg_ms,
            )
        )
    return TrendDTO(granularity=grain, start_time=start, end_time=end, points=points)


def format_bucket(grain: Granularity, value: Any) -> str:
    """桶标签。**由后端格式化**，前端不造 —— 格式漂移会让 ECharts 的 x 轴错位。"""
    if grain == "hour":
        moment = value if isinstance(value, datetime) else _normalize_bucket("hour", value)
        return moment.strftime("%Y-%m-%d %H:00")
    day = value if isinstance(value, date) else _normalize_bucket("day", value).date()
    return day.strftime("%Y-%m-%d")


async def get_top_questions(
    db: AsyncSession, query: DashboardQuery
) -> list[TopQuestionItem]:
    """高频问题榜：合并窗口内各桶的 JSON 快照，再排序取前 K。

    ⚠️ 这是**近似**（be-07 §4.3 / 字典 §9 #51）：某个问题若在各桶都排第
       `stats_top_n + 1` 名，跨桶合并时会漏掉，而它本可能进总榜。
       对运营看板可接受 —— 精确 TOP 需要另开维度表，PRD §8.5 只点名两张表。
    """
    start, end = resolve_window(query.start_time, query.end_time)
    grain = _pick_model(start, end, None)
    rows = list((await db.execute(_window_select(grain, start, end))).scalars())

    merged: dict[str, int] = defaultdict(int)
    for row in rows:
        for item in row.top_questions or []:
            merged[str(item.get("question", ""))] += int(item.get("count", 0) or 0)

    ranked = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[: query.limit]
    return [TopQuestionItem(question=q, count=c) for q, c in ranked if q]


async def get_top_knowledge(
    db: AsyncSession, user: AuthContext, query: DashboardQuery
) -> list[TopKnowledgeItem]:
    """知识热度榜。**含角色硬闸**（红线 4，`P2-13-be-stats` §4.4）。"""
    await assert_admin_scope(db, user, "top-knowledge")

    start, end = resolve_window(query.start_time, query.end_time)
    grain = _pick_model(start, end, None)
    rows = list((await db.execute(_window_select(grain, start, end))).scalars())

    merged: dict[str, int] = defaultdict(int)
    for row in rows:
        for item in row.top_documents or []:
            merged[str(item.get("document_id", ""))] += int(item.get("cite_count", 0) or 0)

    ranked = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[: query.limit]
    if not ranked:
        return []

    # 回填标题。⚠️ 软删的文档**不出现**在榜上：标题已属于「不应再展示」的内容，
    # 且榜的意义是「现在该补哪份文档」，已删的文档没有行动价值。
    stmt = select(KbDocument.id, KbDocument.title).where(
        KbDocument.id.in_([doc_id for doc_id, _ in ranked]),
        KbDocument.deleted_at.is_(None),
    )
    titles = {r.id: r.title for r in (await db.execute(stmt)).all()}

    # ⚠️ 标题查不到的**静默跳过**，不补空串：空标题在榜上是一条无信息的行，
    #    用户会以为界面坏了。少一行比一行空白好。
    return [
        TopKnowledgeItem(document_id=doc_id, title=titles[doc_id], cite_count=count)
        for doc_id, count in ranked
        if doc_id in titles
    ]


async def assert_admin_scope(db: AsyncSession, user: AuthContext, endpoint: str) -> None:
    """`top-knowledge` 的角色硬闸（红线 4）。

    🔴 **为什么需要它**：该端点返回**文档标题**。当前 `stats:dashboard:view`
       只授 `sys_admin` / `kb_admin`，管理员台账访问不受数据权限约束（PRD §3.3），
       故合规 —— **但这是会过期的前提**：哪天有人把该码授给 `staff`，
       本端点立刻变成静默越权泄漏点（靠热度榜就能推断出自己无权查看的文档
       **存在**，红线 4）。显式校验角色能把「未来的授权失误」变成**响亮的 403**，
       而不是一条安静的泄漏。

    ⚠️ **为什么用「拒绝」而不是「按数据权限过滤」**：过滤版本要为榜上每篇文档算一次
       可见性（四维 ACL + 部门继承），而榜是**聚合结果**，过滤后 `cite_count`
       会与 `overview.pv` 对不上（数字自相矛盾）。拒绝则把问题挡在门口，语义干净。

    `user.roles` 存的是**角色 id**，所以要回表取 `code` 比对。
    """
    if not user.roles:
        raise ForbiddenError("无权访问知识热度榜")
    stmt = select(SysRole.code).where(SysRole.id.in_(user.roles))
    codes = set((await db.execute(stmt)).scalars())
    if not (codes & set(settings.stats_admin_role_list)):
        logger.warning(
            "非管理员角色访问 %s 被拒：roles=%s（红线 4 的角色硬闸）", endpoint, sorted(user.roles)
        )
        raise ForbiddenError("无权访问知识热度榜")


async def get_latency_histogram(db: AsyncSession, query: DashboardQuery) -> LatencyHistogramDTO:
    """延时直方图 + 均值 + P95。"""
    start, end = resolve_window(query.start_time, query.end_time)
    grain = _pick_model(start, end, None)
    rows = list((await db.execute(_window_select(grain, start, end))).scalars())

    edges = settings.stats_latency_edges
    merged: dict[str, int] = defaultdict(int)
    for row in rows:
        for key, count in (row.latency_buckets or {}).items():
            merged[str(key)] += int(count or 0)

    pv = sum(r.pv for r in rows)
    weighted = sum(r.latency_avg_ms * r.pv for r in rows)

    return LatencyHistogramDTO(
        edges=edges,
        # 🔴 `counts.length == edges.length - 1`：**末桶是开区间**。
        #    这里按 `edges[:-1]` 逐桶取值，天然保证长度等于 `len(edges) - 1`。
        counts=[merged.get(str(e), 0) for e in edges[:-1]],
        avg_ms=int(weighted / pv) if pv else 0,
        p95_ms=p95_from_histogram(dict(merged), edges),
    )
