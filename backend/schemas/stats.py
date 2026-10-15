"""运营看板域 DTO（be-07）。

对齐 `frontend/src/types/stats.ts` —— 那个文件是**先写的**，本文件是它的后端对应物。
字段名不一致的后果不是编译错误，而是前端 `data.today_question_count` 取到 `undefined`，
KPI 卡显示 `NaN` 或空 —— **接口照回 200**。所以这里每一个名字都要能逐字对上。
（详见 `P2-13-be-stats` §4.2 与 be-07 §3.1 的修订表。）

🔴 全部 ID 字段为 `str`（字典 §1：主键一律字符串，库里是 VARCHAR）。
🔴 比例字段（`faq_hit_rate` / `coverage_rate`）一律 **0–1 的小数**，不是百分比。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from backend.core.settings import settings

__all__ = [
    "BucketRow",
    "DashboardQuery",
    "LatencyHistogramDTO",
    "OverviewDTO",
    "TopKnowledgeItem",
    "TopQuestionItem",
    "TrendDTO",
    "TrendPoint",
]

Granularity = Literal["hour", "day"]


# ═══════════════════════════════════════════════════════════════
# 查询条件
# ═══════════════════════════════════════════════════════════════


class DashboardQuery(BaseModel):
    """看板查询的时间窗（5 个端点共用）。

    🔴 **参数名与格式是冻结契约**（全局 #77 / fe-05 §9-2）：
       时间格式 `YYYY-MM-DD HH:mm:ss` 或 `YYYY-MM-DD`，**naive，Asia/Shanghai**。

    ⚠️ **为什么这件事必须写死在契约里**：参数名对不上时，FastAPI 不认识的多余
       query 参数**会被静默忽略**（不是 422 —— 它们根本不在签名里，进不了校验）。
       症状是「时间范围控件看起来能用，但结果永远是默认窗口」，**没有任何报错**。
       守门用例：`verify_stats.py` T2-11（发一个近 30 天的窗口，断言响应回显
       的 `start_time` 与请求一致，而**不是**默认 7 天）。
    """

    start_time: datetime | None = Field(
        None, description="起始时间（含），naive Asia/Shanghai；不传则取近 N 天"
    )
    end_time: datetime | None = Field(None, description="结束时间（含），同上；不传则取当前时刻")
    # 上限走配置而不是字面量 50：`limit` 是「一次把整桶 JSON 拉出来」的成本阀。
    limit: int = Field(10, ge=1, le=settings.stats_top_n, description="榜单项数")


class TrendQuery(DashboardQuery):
    """`trend` 的查询条件 = 公共时间窗 + 粒度。

    ⚠️ **没有 `metric` 参数**（be-07 §3.1 修订表第 2 条）：趋势图是双系列
       （Token 消耗 + 提问量）共用一根柱，单 metric 模型会逼前端发 2~3 次请求
       再在前端拼桶 —— 拼桶是聚合，违反 fe-05 §1.2「不做任何聚合计算」。
    """

    granularity: Granularity = Field("day", description="桶粒度；hour 只允许查近 N 天")


# ═══════════════════════════════════════════════════════════════
# 聚合中间产物
# ═══════════════════════════════════════════════════════════════


class BucketRow(BaseModel):
    """一个时间桶算出来的全部指标，**UPSERT 的输入**。

    🔴 **用模型而不是裸 dict 跨层传**（CLAUDE.md 分层铁律）：聚合函数返回
       `list[BucketRow]`，UPSERT 按字段名取值。用裸 dict 时，聚合里少算一个键
       与 UPSERT 里写错一个键名，后果都是「写进库里一列 0」——**不报错**。
       模型化让缺字段在构造期就炸出来。

    `bucket` 是**已格式化好的时间桶标识**（hour → `datetime`；day → `date`），
    由聚合侧决定，UPSERT 侧不再做时间运算。
    """

    bucket: datetime
    pv: int = 0
    uv: int = 0
    session_count: int = 0
    faq_hit_count: int = 0
    no_hit_count: int = 0
    token_in_sum: int = 0
    token_out_sum: int = 0
    latency_avg_ms: int = 0
    latency_p95_ms: int = 0
    latency_buckets: dict[str, int] = Field(default_factory=dict)
    blocked_count: int = 0
    top_questions: list[dict[str, Any]] = Field(default_factory=list)
    top_documents: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def faq_hit_rate(self) -> float:
        """派生而不是存储：`faq_hit_count / pv`。

        ⚠️ 写成**属性**而不是字段，是为了让它不可能与 `faq_hit_count` / `pv` 不一致 ——
           存一份派生值，就有「改了分子忘了改比值」这个失效模式。
           `pv=0` 时返回 0（不是 `ZeroDivisionError`，也不是 `NaN`：
           `NaN` 序列化进 JSON 会变成 `null`，前端拿到 `null` 会走 `??` 兜底显示 `0%`，
           看起来对，但那是巧合；显式给 0 才是契约）。
        """
        return self.faq_hit_count / self.pv if self.pv else 0.0


# ═══════════════════════════════════════════════════════════════
# 出参
# ═══════════════════════════════════════════════════════════════


class OverviewDTO(BaseModel):
    """KPI 卡数据。

    ⚠️ **各字段的时间口径不同，这是刻意的**（be-07 §3.1 的「字段口径」表）：

    | 字段 | 窗口 |
    |---|---|
    | `pv` / `uv` / `faq_hit_*` / `no_hit_count` / `blocked_count` / 延时 | **所选范围** |
    | `document_count` | **全量**（不随范围变） |
    | `today_question_count` | **当日**（不随范围变） |

    ⇒ 因此响应**必须回带生效窗口**（`start_time` / `end_time`）：不回带的话，
       用户切范围时看到「有的卡变了、有的没变」，会以为页面坏了。
    """

    start_time: datetime = Field(..., description="生效窗口起点（未传时补默认值后的结果）")
    end_time: datetime = Field(..., description="生效窗口终点")
    updated_at: datetime | None = Field(
        None, description="所查桶中最近一次聚合时间；无数据时 null（前端据此显示「数据截至 …」）"
    )
    granularity: Granularity = Field(..., description="实际查的是小时表还是日表")

    pv: int = Field(0, description="提问量（人次）")
    uv: int = Field(0, description="去重人数；多桶窗口下是各桶之和，见 uv_is_exact")
    uv_is_exact: bool = Field(
        False,
        description=(
            "uv 是否可当「人数」读。true ⟺ 窗口落在恰好一个桶内。"
            "🔴 不许省略这个字段：只回一个数字，前端无从判断能不能加「人」字，"
            "而把「人次」当「人数」展示是一个**必然发生**的误读。"
        ),
    )

    document_count: int = Field(0, description="知识单元总数（全量，deleted_at IS NULL）")
    today_question_count: int = Field(0, description="今日提问量（当日，不随所选范围变）")

    faq_hit_count: int = Field(0, description="FAQ 命中条数（所选范围）")
    faq_hit_rate: float = Field(
        0.0, ge=0, le=1, description="FAQ 命中率，**0–1 的小数**（不是百分比）"
    )
    blocked_count: int = Field(0, description="权限受限拦截条数（RED LINE 9 侧信道触发次数）")
    no_hit_count: int = Field(0, description="未命中提问数")
    coverage_rate: float = Field(
        0.0, ge=0, le=1, description="知识覆盖率 = 1 - no_hit_count/pv，**0–1 的小数**"
    )

    avg_latency_ms: int = Field(
        0, description="平均响应耗时（ms）。🔴 按 pv 加权的整体均值，不是各桶 avg 的算术平均"
    )
    latency_p95_ms: int = Field(
        0, description="P95 响应耗时（ms）。⚠️ 由延时直方图**插值估算**，桶边界越密越准"
    )


class TrendPoint(BaseModel):
    """趋势图的一个桶。

    ⚠️ `bucket` 是**后端格式化好的标签**（day → `2026-10-14`；hour → `2026-10-14 15:00`），
       前端不自己造 —— 格式漂移（比如某天变成 `2026/10/14`）会让 ECharts 的 x 轴错位。
    """

    bucket: str = Field(..., description="桶标签，由后端格式化")
    token_in: int = 0
    token_out: int = 0
    question_count: int = 0
    avg_latency_ms: int = 0


class TrendDTO(BaseModel):
    """趋势序列。

    ⚠️ `points` **只包含有数据的桶**，不补空洞（字典 §9 #124）。
       补空桶需要后端决定「从哪补到哪」，而 `start_time`/`end_time` 已在响应里，
       前端补更准确。代价：两个范围不同的响应点数不同 ⇒
       前端**不能按数组下标对齐**两条曲线，必须按 `bucket` 字符串对齐。
    """

    granularity: Granularity
    start_time: datetime
    end_time: datetime
    points: list[TrendPoint] = Field(default_factory=list)


class TopQuestionItem(BaseModel):
    """高频问题榜的一行。

    ⚠️ `question` 是**用户原始输入**（未脱敏）。安全红线 2 的落地在**前端**：
       渲染必须走插值（`{{ }}`），禁止 `v-html`。后端不做富文本包装 ——
       一旦后端加了 `<em>` 之类的标记，前端就有理由上 `v-html` 了。
    """

    question: str
    count: int


class TopKnowledgeItem(BaseModel):
    """知识热度榜的一行。

    ⚠️ `title` 是**文档标题**。安全红线 4 要求「未授权内容不返回标题」——
       本端点当前只对 `sys_admin` / `kb_admin` 开放（PRD §3.3 管理员台账访问不受
       数据权限约束），故合规；但 `stats_service.get_top_knowledge` **额外**做了
       角色硬闸，见 `P2-13-be-stats` §4.4。
    """

    document_id: str
    title: str
    cite_count: int


class LatencyHistogramDTO(BaseModel):
    """响应延时直方图 + 均值 + P95。

    🔴 **`counts.length == edges.length - 1`，最后一个桶是开区间**（`>= 末边界`）。
       少了这个开区间，「超过 5 秒」的请求会**全部落空**，
       直方图总数 ≠ `pv`，而**没人会发现**（图看起来只是矮了一点）。
       守门用例：`verify_stats.py` T1-24。
    """

    edges: list[int] = Field(..., description="桶边界（ms），升序，最后一个为开区间")
    counts: list[int] = Field(..., description="各桶计数，长度 = edges.length - 1")
    avg_ms: int = Field(0, description="按 pv 加权的平均耗时")
    p95_ms: int = Field(0, description="P95 耗时；⚠️ 直方图插值估算，非精确分位数")

    @model_validator(mode="after")
    def _check_shape(self) -> "LatencyHistogramDTO":
        """把「`counts` 与 `edges` 对不上」变成**构造期**的错误。

        🔴 为什么不只写在注释里：`counts` 比 `edges` **短**一项时，前端按
           `edges[i]~edges[i+1]` 逐段画图，**最后那段会画成 0**——
           图表看起来完全正常，只是尾巴矮了一截。少的是「最慢的那批请求」，
           恰恰是排障最想看的。没人会怀疑一个 0。
           （同理：多一项时 `edges` 越界取 `undefined`，画出来是 NaN 柱。）

        校验放在 schema 而不是 service：service 里三处组装这个 DTO
        （`get_latency_histogram` / `get_overview` / 将来的导出），
        放在 schema 让三处**同时**受保护，不必记得每处都写断言。
        """
        if len(self.counts) != len(self.edges) - 1:
            raise ValueError(
                f"counts 长度必须为 edges 长度 - 1（末桶为开区间）；"
                f"实测 edges={len(self.edges)} counts={len(self.counts)}"
            )
        return self
