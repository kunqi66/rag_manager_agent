"""数据挖掘域 DTO（be-08 FAQ 挖掘 / be-09 缺口闭环）。

对齐 `frontend/src/types/governance.ts` —— 那个文件**先于本文件存在**，
且逐字段标了 🟡/❌ 的未定标记。**它就是本模块的验收清单**。

⚠️ 字段名不一致的后果不是编译错误：前端 `data.question_count` 取到 `undefined`，
指标卡显示 `NaN` 或空 —— **接口照回 200**。所以每个名字都要能逐字对上
（同 be-07 `schemas/stats.py` 的教训，见 `P2-13` §10.1 #133）。

🔴 全部 ID 字段为 `str`（字典 §1：主键一律 VARCHAR）。
🔴 比例字段（`confidence`）是 **0–1 的小数**，不是百分比。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.core.settings import settings

__all__ = [
    "CandidateQuery",
    "CandidateReviewDTO",
    "CandidateStatus",
    "FaqCacheDTO",
    "FaqCandidateItemDTO",
    "FaqItemDTO",
    "FaqQuery",
    "FaqReviewPayload",
    "GapAggregateResultDTO",
    "GapQuery",
    "GapStatus",
    "GapTaskResult",
    "KnowledgeGapItemDTO",
    "MineResultDTO",
    "MiningOverviewDTO",
    "RelatedDocBrief",
]

CandidateStatus = Literal["pending", "published", "rejected"]

#: 缺口状态（字典 §5.4 ✅）。与前端 `governance.ts` 的 `GapStatus` 逐字一致 ——
#: 这两处对不上**不是编译错误**，只会让联合类型收窄失效（TS 里落到 `never`）。
GapStatus = Literal["open", "tasked", "closed"]


# ═══════════════════════════════════════════════════════════════
# 读模型
# ═══════════════════════════════════════════════════════════════


class RelatedDocBrief(BaseModel):
    """关联知识单元的精简信息（`frontend/src/types/governance.ts` 同名接口）。

    🔴 **`title` 的可见性受数据权限约束**（CLAUDE.md 红线 4）。
       无权看的文档在这里**整条不出现**，而不是把 `title` 置空 ——
       置空会在前端留一个空框，等于确认「这里有一篇你看不到的文档」。
       落地点见 `engines/data_mining_engine/faq_mining.list_candidates`。
    """

    id: str
    title: str


class FaqCandidateItemDTO(BaseModel):
    """候选 FAQ 条目。

    ⚠️ **不含 `question_embedding`**。它只服务后端的进程内索引与相似度聚合，
       对展示层毫无用途 —— 前端类型里也刻意排除了它。从契约上表达「它不属于前端」，
       比在文档里写一句「别展示」可靠。
    """

    id: str
    #: 聚类簇标识。**仅用于列表 key 的稳定，不展示**
    cluster_key: str
    #: 代表性问题（簇内距质心最近那条的原文）
    question: str
    #: 推荐标准答案。⚠️ **属 LLM 输出** ⇒ 前端必须走插值渲染而非 `v-html`（红线 2）
    answer: str
    frequency: int
    #: 0–1
    confidence: float
    #: 关联知识单元的 **document_id** 数组
    related_doc_ids: list[str] = Field(default_factory=list)
    #: 后端内联的标题。**无权看的已剔除**，因此可能比 `related_doc_ids` 短
    related_docs: list[RelatedDocBrief] = Field(default_factory=list)
    status: CandidateStatus
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime


class FaqItemDTO(BaseModel):
    """已发布 FAQ（`frontend/src/types/governance.ts` 的 `FaqItem`）。"""

    id: str
    question: str
    answer: str
    #: 是否参与进程内索引的毫秒级匹配（PRD §6.3.2）
    cache_enabled: bool
    #: 来源候选 ID。**本模块不提供手工新建**，故按设计它总是有值；
    #: 保留可空是给「候选行被清理」留余地，不是给手工建留的。
    source_candidate_id: str | None = None
    published_by: str
    published_at: datetime
    #: 缓存机制的效果度量
    hit_count: int


class MiningOverviewDTO(BaseModel):
    """治理中心顶部统计块（`GET /api/mining/stats/overview`）。

    🔴 **5 个字段全部投影自 be-07 的 `stats_service.get_overview()`，不写第二条聚合 SQL。**
       两处各写一份聚合，必然出现「治理中心的覆盖率 0.82、看板的 0.79」这种
       **双方都自洽、都不报错**的裂开，而排查者不知道该信哪个。
       （`P3-14` §4.2 / 字典 §9.2 #140）

    ⚠️ 与 `OverviewDTO` 的区别只是**字段改名 + 去掉看板专属的时间窗字段**：
       这里是「最近一段时间的概览」，不暴露粒度与起止时间，因为本页不提供时间筛选。
    """

    #: = `OverviewDTO.pv`
    question_count: int
    #: = `OverviewDTO.uv`。🔴 是**人次/桶内去重**口径，跨桶不可加（字典 §2.5 的告诫）
    unique_user_count: int
    #: = `OverviewDTO.faq_hit_rate`，0–1
    faq_hit_rate: float
    #: = `OverviewDTO.coverage_rate`，0–1
    kb_coverage: float
    #: = `OverviewDTO.avg_latency_ms`，**pv 加权**而非各桶均值的算术平均
    avg_latency_ms: float


# ═══════════════════════════════════════════════════════════════
# 写模型
# ═══════════════════════════════════════════════════════════════


class FaqReviewPayload(BaseModel):
    """审核请求体 —— **与 `data_mining_router.py` 壳子里的同名类严格同构**。

    `question` / `answer` 是**人工修正后的当前值**：用户没改也要原样提交，
    后端以请求体为准，**不做「空值就用原推荐答案」的兜底猜测** ——
    否则用户清空答案后反而会发布出旧答案（fe-06 §3）。

    🔒 **不得加任何身份字段**（`reviewer_id` 等）：审核人由后端从 JWT 取（红线 1）。
       `extra="forbid"` 让「客户端硬塞一个 `reviewer_id`」变成 422 而不是被静默忽略 ——
       静默忽略会让人误以为这个字段生效了。
    """

    model_config = {"extra": "forbid"}

    action: Literal["publish", "reject"]
    question: str = Field(min_length=1, max_length=1000)
    answer: str = Field(min_length=1, max_length=8000)


class CandidateReviewDTO(BaseModel):
    candidate_id: str
    status: CandidateStatus
    #: 发布时产生的新 FAQ ID；驳回时为 `None`
    faq_id: str | None = None


class FaqCacheDTO(BaseModel):
    faq_id: str
    cache_enabled: bool


class MineResultDTO(BaseModel):
    """手动挖掘的响应。

    `window` 是后端自查用的（前端类型里没有它，但 pydantic 多一个字段不影响前端）。
    保留它是因为「挖了多久的窗口」直接决定候选数多不多，排障时第一个要问的就是它。
    """

    candidates_created: int = 0
    #: 扫过的簇数（含被 `min_frequency` 滤掉的），用于判断阈值是否过高
    clusters_scanned: int = 0
    #: 实际挖掘窗口的起止（`YYYY-MM-DD HH:mm:ss`）
    window_start: datetime | None = None
    window_end: datetime | None = None
    #: 🔴 是否因超时而**只挖了部分窗口**。前端据此把文案退化为「已挖掘近 N 天」。
    #: 见 `P3-14` §5.6：手动挖掘是同步慢接口，而 `http.ts` 全局超时 15 s。
    partial: bool = False


# ═══════════════════════════════════════════════════════════════
# 查询参数
# ═══════════════════════════════════════════════════════════════


class CandidateQuery(BaseModel):
    """候选列表的筛选 + 分页。

    🔴 **`page` / `page_size` 是补的**：路由器壳子原只声明了 `status` / `min_confidence`，
       而 **fe-06 已经在发分页参数**。FastAPI 对不在签名里的 query 参数是**静默忽略**
       （不是 422）⇒「翻页点了没反应，永远第一页」，且不报任何错。
    """

    status: CandidateStatus = "pending"
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    page: int = Field(default=1, ge=1)
    # 🔴 上限用 `le=` **拒绝**，不是 `min()` 静默截断 —— 与 org / kb / audit / qa
    #    四域的既有写法一致（它们全都是 `le=settings.x_page_size_max`）。
    #    截断的代价是「客户端要 100000 条，拿到 20 条，且**没有任何信号**」：
    #    响应里的 `page_size` 是**被改过的**值，调用方拿它跟自己的请求参数一比
    #    才会发现，而正常代码不会去比。要全表读就该显式改上限配置。
    page_size: int = Field(default=20, ge=1, le=settings.faq_page_size_max)


class FaqQuery(BaseModel):
    """已发布 FAQ 的分页 + 关键词。"""

    keyword: str | None = Field(default=None, max_length=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=settings.faq_page_size_max)


# ═══════════════════════════════════════════════════════════════
# 知识缺口（be-09）
# ═══════════════════════════════════════════════════════════════


class KnowledgeGapItemDTO(BaseModel):
    """一条知识缺口（字典 §2.4 `knowledge_gap`）。

    🔴 **不定义 `question_embedding`** —— 它只服务后端聚合，对展示层毫无用途；
       从类型里排除就是从契约上表达「它不属于前端」（同 `FaqCandidateItemDTO`）。

    🔴 **也没有 `user_id`**（表里就没有）：缺口是**部门**粒度的运营数据，
       落提问人会让治理页变成「谁问了什么」的人员监控视图。

    🟡 **没有 `department_name`**：缺口可能来自**别的**部门，前端拿不到它的名字
       （前端只有自己那条部门链）。字典 §9 #146 已决定撤回该字段，
       由展示层降级显示 `department_id`。要显示部门名需要 be-02 另开取名接口。
    """

    id: str
    question: str
    department_id: str | None = None
    frequency: int
    max_score: float | None = None
    suggested_category: str | None = None
    status: GapStatus
    last_seen_at: datetime
    created_at: datetime


class GapQuery(BaseModel):
    """缺口清单的筛选 + 分页。

    ⚠️ `min_frequency` 的下界是 **1 而不是 0**（字典 §9 #147）：
       `frequency` 恒 ≥ 1（新缺口即 1），传 0 与传 1 结果**完全相同**，
       允许 0 只会让「0 是不是表示不过滤」变成下一个人要猜的问题。
    """

    min_frequency: int = Field(default=1, ge=1)
    page: int = Field(default=1, ge=1)
    # 上限走配置，且与其它五域一样是 `le=` **拒绝**（理由见 `CandidateQuery.page_size`）。
    page_size: int = Field(default=20, ge=1, le=settings.gap_page_size_max)


class GapTaskResult(BaseModel):
    """一键转建的响应。

    🔴 **`task_id` 必回**（不是可选）：前端要拿它跳到导入中心，
       壳子 `data: None` 是待实现态。少回它前端只能给一句「操作成功」，
       而用户下一步该干什么无从得知。
    """

    gap_id: str
    status: GapStatus
    task_id: str


class GapAggregateResultDTO(BaseModel):
    """一轮聚合的结果（Celery 任务的返回值，也是排障入口）。

    `skipped_by_acl` 单列是**刻意的**：它是「某部门权限配少了」的强信号 ——
    那些提问有召回、但候选全被权限挡住。它们**不计入缺口池**（缺的是权限不是知识，
    见 `gap.is_miss`），但必须能看见，否则「昨天少了 30 条未命中」无人能解释。
    """

    scanned: int = 0
    created: int = 0
    updated: int = 0
    closed: int = 0
    skipped_by_acl: int = 0
    window: dict[str, str] = Field(default_factory=dict)
