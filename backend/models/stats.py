"""运营看板预聚合表（字典 §2.5）。

**这两张表存在的唯一理由**是 PRD §8.5 的硬要求：看板**只查聚合表**，不实时扫 `qa_log`。
它们是「可重跑的物化视图」——每一轮任务把源数据整桶**重算并覆盖**，而不是在旧值上累加
（be-07 §4.2）。所以本文件里**没有任何自增/累加语义的列**，所有指标列都是「这一桶算出来的值」。

两表**指标列完全同构**，只有时间桶列与颗粒度不同，聚合代码按 `grain` 复用同一段。

🔴 **`uv` 是桶内去重人数，跨桶不可加**。`SUM(uv)` 得到的是**人次**不是**人数**。
   看板侧用 `uv_is_exact` 告诉前端这个数能不能当「人数」读（`P2-13-be-stats` §4.2）。
   这不是实现细节 —— 它是「数字看起来对但其实错」的典型，且没人会对一个正常的整数起疑。
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base

__all__ = ["StatsHourly", "StatsDaily"]


class StatsHourly(Base):
    """小时预聚合。唯一键 `bucket_start`（整点，naive Asia/Shanghai）。

    保留期 `stats_hourly_retention_days`（默认 90 天）—— 它只服务「近 48h」这类
    细粒度查询，长期趋势走 `stats_daily`。被清理后不影響日表，因为日表**直连 `qa_log`
    重算**而不是从小时桶派生（be-07 §4.2）。
    """

    __tablename__ = "stats_hourly"
    __table_args__ = (
        # 唯一键**不做成主键**：主键是 `id`（全库 ID 形态统一，字典 §1）。
        # 用 UNIQUE 表达「一桶一行」，同时给 UPSERT 一个明确的冲突目标。
        UniqueConstraint("bucket_start", name="uq_stats_hourly_bucket_start"),
        TABLE_KWARGS,
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 整点时间桶起点。**naive**，与 `qa_log.created_at` 同一口径（全链路只用
    #: Asia/Shanghai 的 naive 时间；某处引入 UTC 感知时间会让桶边界整体偏 8 小时，
    #: 症状是「今天的数据跑到昨天去」，且不报错）。
    bucket_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    #: 提问量（`qa_log` 行数，人次）
    pv: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: **桶内**去重提问用户数（人数）。跨桶相加 = 人次，见模块头。
    uv: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: **桶内**去重会话数
    session_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: FAQ 命中条数（`faq_hit = true`）
    faq_hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: `faq_hit_count / pv`。**0–1 的小数**（不是百分比）。`pv=0` 时为 0。
    faq_hit_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: 未命中提问数（`allowed_ids` 空 **或** `top_score < kb_min_score`）。
    #: 供「知识覆盖率」用（字典 §9 #69：覆盖率由本表算，缺口池由 be-09 作明细下钻）。
    no_hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    token_in_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    token_out_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    #: 桶内 `AVG(latency_ms)`。⚠️ 跨桶求整体平均时**必须按 `pv` 加权**，
    #: 各桶 avg 取算术平均会把整体均值拉偏几倍且看起来完全正常（字典 §9 #119）。
    latency_avg_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: P95 耗时（**直方图插值估算**，不是精确分位数 —— MySQL 8 没有 `PERCENTILE`，
    #: 且为它单独扫一遍 `qa_log` 不值得，字典 §9 #117）。
    latency_p95_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 延时直方图 `{"0": 96, "200": 142, ...}`，键是桶下界**字符串**
    #: （JSON 对象的键只能是字符串），桶边界取 `stats_latency_edges_ms`。
    latency_buckets: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    #: 权限受限拦截条数 = `Σ len(blocked_ids)`，即 RED LINE 9 侧信道被触发的次数。
    #: `blocked_ids` 存的是 **document id**，但这里只是数个数，粒度不影响（字典 §9 #113）。
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: 桶内高频问题 TOP N `[{"question": ..., "count": ...}]`。
    #: 同义不同字算两条 —— 语义去重是 be-08 的 FAQ 聚类职责，聚合层做会把分钟级任务变成小时级。
    top_questions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: 桶内高频引用知识 TOP N `[{"document_id": ..., "cite_count": ...}]`。
    #: 🔴 由 `qa_log.allowed_ids`（**chunk id**）JOIN `kb_chunk` 换回 `document_id` 后计数；
    #: 直接把 chunk id 当 document id 用会算错文档热度且**不报错**（字典 §9 #113）。
    top_documents: Mapped[list | None] = mapped_column(JSON, nullable=True)

    #: 本桶最近一次重算时间。排障用：判断桶是否已刷新。
    #: 静默失败的唯一可观测手段 —— beat 停一晚，这里就停在昨晚（字典 §9 #123）。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class StatsDaily(Base):
    """日预聚合。唯一键 `bucket_date`（当日 00:00:00 的 DATE）。

    **同样从 `qa_log` 重算**（`GROUP BY DATE(created_at)`），而不是「把当天 24 个
    hourly 加起来」：小时表有保留期会被清理，一旦被清理，从它派生日表就会**静默算少**。
    两者都直连源表，互不依赖，最稳。代价是每轮多扫一个「当天」的区间，量级很小。
    """

    __tablename__ = "stats_daily"
    __table_args__ = (
        UniqueConstraint("bucket_date", name="uq_stats_daily_bucket_date"),
        TABLE_KWARGS,
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 日期桶（naive Asia/Shanghai 日界）。此口径下 `uv` 即**当日**去重人数，
    #: 可以直接当「人数」展示；但**跨日求和仍是人次**（字典 §9 #122）。
    bucket_date: Mapped[date] = mapped_column(Date, nullable=False)

    pv: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uv: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    session_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    faq_hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    faq_hit_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    no_hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_in_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    token_out_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    latency_avg_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_p95_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_buckets: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top_questions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    top_documents: Mapped[list | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
