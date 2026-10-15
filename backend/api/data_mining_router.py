"""数据挖掘与知识沉淀路由。

对应引擎：backend/engines/data_mining_engine
职责：会话日志聚类、高频问题挖掘、候选 FAQ 推荐与人工审核发布、
      FAQ 高速缓存生命周期管理、知识缺口识别与运营指标聚合。

🔴 **本层只做四件事**：解析请求 → 注入身份与 session → 查权限 → 转发给引擎/服务。
   不写业务逻辑、不写 SQL（CLAUDE.md 分层铁律）。

🔴 **`AuthContext` 从 JWT 来**（`deps.get_current_user`），
   **不存在任何客户端可传的身份字段**（PRD §11.3 R3 / 红线 1）。
   本文件里找不到 `user_context` / `reviewer_id` 之类的入参，这是**刻意的**。

端点分组（PRD §8.4 保持 8 个端点不变）：
- be-08（本文件：5 个 FAQ 端点 + 1 个运营摘要）
- be-09（本文件：2 个缺口端点 —— 清单 + 一键转建）
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.db import get_db
from backend.core.deps import get_current_user, require_permission
from backend.core.exceptions import ParamError
from backend.core.response import ok, page_result
from backend.core.settings import settings
from backend.engines.data_mining_engine import faq_mining, gap
from backend.schemas.auth import AuthContext
from backend.schemas.mining import (
    CandidateQuery,
    CandidateReviewDTO,
    FaqCacheDTO,
    FaqCandidateItemDTO,
    FaqItemDTO,
    FaqQuery,
    FaqReviewPayload,
    GapQuery,
    KnowledgeGapItemDTO,
    MineResultDTO,
    MiningOverviewDTO,
    RelatedDocBrief,
)
from backend.schemas.stats import DashboardQuery
from backend.service import faq_service, stats_service

router = APIRouter(prefix="/api/mining", tags=["数据挖掘与沉淀"])


# ═══════════════════════════════════════════════════════════════
# FAQ 挖掘（be-08）
# ═══════════════════════════════════════════════════════════════


@router.post("/faq/mine", summary="触发高频问题聚类挖掘")
async def mine_faq(
    start_time: str = Query("", description="日志起始时间，为空则取上次挖掘位点"),
    end_time: str = Query("", description="日志结束时间"),
    min_frequency: int = Query(5, ge=1, description="生成推荐 FAQ 的最小聚合频次阈值"),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("faq:candidate:mine")),
):
    """对历史提问日志执行语义去重与频次聚合，生成候选 FAQ 推荐项。

    ## 为什么是同步跑（be-08 §5.6 已拍板，此处不重开）

    本端点的定位是「演示与手动补跑」（常态是 beat 每日 02:37），
    演示要的是「点了按钮、等几秒、看到新增 12 个候选」，
    回一个 `task_id` 反而还要再轮询一次。

    ⚠️ **但 `http.ts` 的全局超时是 15 s**，聚类又是 O(N²) 的 CPU 密集活。
       所以：超时上限走 `FAQ_MINE_SYNC_MAX_SECONDS`（默认 10 s，**刻意小于 15**），
       且**只在超限时截短窗口并回带 `partial: true`** —— 宁可少挖一点、
       让前端把文案退化成「已挖掘近 N 天」，也不要让用户对着超时发呆。

    ⚠️ 起止时间目前**解析后即忽略**（`start_time`/`end_time` 传了也用默认窗口）：
       fe-06 的挖掘按钮不传这两个参数（它只要「挖最近一段」），
       而支持任意窗口要一套时间解析 + 校验（且 `str` 入参的格式错误会变成 500）。
       与其留一个「传了但不生效」的静默坑，不如显式报 1001。
    """
    if start_time or end_time:
        raise ParamError("暂不支持自定义挖掘窗口，请留空（走 MINING_LOOKBACK_DAYS）")

    began = time.monotonic()
    result = await faq_mining.mine_faq_clusters(db, min_frequency=min_frequency)
    payload = MineResultDTO(**result)
    # 时限**只用于标注 `partial`，不用来中断**：
    # 真正的耗时保护是引擎里的「单轮最多扫 `_MAX_LOGS` 条」，
    # 这里只负责在事后告诉调用方「这一轮跑得偏久，数据可能不全」。
    # 反过来「超时就丢弃结果」会让运营点两次按钮得到 0 条新增
    # （第一次的候选已经写进库了），看起来像功能坏了。
    if time.monotonic() - began > settings.faq_mine_sync_max_seconds:
        payload.partial = True
    return ok(payload.model_dump())


@router.get("/faq/candidates", summary="候选 FAQ 推荐列表")
async def list_faq_candidates(
    query: CandidateQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("faq:candidate:list")),
):
    """返回聚类问题簇、聚合频次、关联知识单元、推荐标准答案与置信度得分。

    🔴 **读端点也挂权限码**。它和 `kb:*` 的读端点不同：候选列表的**问题原文**
       来自全公司的提问日志（聚类窗口是全局的，不按部门切分），
       而侧栏菜单不会按权限过滤 —— 不挂码等于任何登录用户都能读到
       别的部门问了什么。字典 §5.6.4 已把这个取舍写进去了。
    """
    rows, total = await faq_mining.list_candidates(
        db,
        status=query.status,
        min_confidence=query.min_confidence,
        page=query.page,
        page_size=query.page_size,
    )

    items: list[FaqCandidateItemDTO] = []
    for row in rows:
        doc_ids = list(row.related_doc_ids or [])
        # 🔴 标题的权限回填**逐条做**（红线 4）。放在循环里是为了让
        #    `post_verify` 的可见集合查询能被 SQLAlchemy 的 identity map 复用；
        #    实测上候选页最多 20 条，代价可接受。
        related: list[RelatedDocBrief] = await faq_service.attach_related_docs(db, ctx, doc_ids)
        items.append(
            FaqCandidateItemDTO(
                id=row.id,
                cluster_key=row.cluster_key,
                question=row.question,
                answer=row.answer,
                frequency=row.frequency,
                confidence=row.confidence,
                related_doc_ids=doc_ids,
                related_docs=related,
                status=row.status,
                reviewed_by=row.reviewer_id,
                reviewed_at=row.reviewed_at,
                created_at=row.created_at,
            )
        )
    return ok(page_result([item.model_dump() for item in items], total, query.page, query.page_size))


@router.post("/faq/candidates/{candidate_id}/review", summary="审核发布或驳回候选 FAQ")
async def review_faq_candidate(
    candidate_id: str,
    payload: FaqReviewPayload,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("faq:candidate:publish")),
):
    """审核候选 FAQ；发布时同步写入 FAQ 高速应答缓存。

    🔴 **审核人只从 `ctx` 取**（红线 1）。`FaqReviewPayload` 里没有身份字段，
       且它是 `extra="forbid"` —— 客户端硬塞一个 `reviewer_id` 会得到 422，
       而不是被静默忽略（静默忽略会让人误以为那个字段生效了）。

    ⚠️ 权限码是 `publish` 不是 `review`：PRD §3.2 与
       `frontend/src/constants/governance.ts` 都写的是 `publish`，
       字典 §9 #78 曾建议 `review`，以 PRD（SSOT）+ 已落地的前端为准。
       见字典 §5.6.4。
    """
    status, faq_id = await faq_mining.review_candidate(
        db,
        candidate_id=candidate_id,
        action=payload.action,
        question=payload.question,
        answer=payload.answer,
        ctx=ctx,
    )
    return ok(CandidateReviewDTO(candidate_id=candidate_id, status=status, faq_id=faq_id).model_dump())


@router.get("/faq", summary="已发布 FAQ 知识库列表")
async def list_faq(
    query: FaqQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("faq:list")),
):
    """查询已发布的标准化 FAQ 问答对。

    🔴 **仅 `sys_admin` / `kb_admin`**。`FaqItem.answer` 是标准答案本身
       （往往是知识正文的浓缩），按字典 §3.3「管理员台账访问不受数据权限约束」
       的口径，只对管理员开放。普通员工通过**问答命中**拿到答案，
       那是「问到了才有」，不是「可以翻台账」。
    """
    rows, total = await faq_service.list_faqs(db, query)
    return ok(page_result([row.model_dump() for row in rows], total, query.page, query.page_size))


@router.put("/faq/{faq_id}/cache", summary="启用或停用 FAQ 缓存")
async def toggle_faq_cache(
    faq_id: str,
    enabled: bool = Query(..., description="缓存是否生效"),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("faq:cache:update")),
):
    """控制单条 FAQ 的快速应答缓存是否生效。

    ⚠️ `enabled` **必填**（`Query(...)`）。给它默认值的话，
       前端漏传参数会变成「静默打开缓存」—— 而这个开关是运营用来
       「这条答得不对，先别用」的唯一手段，静默打开等于关掉这个手段。

    🔴 `ctx` 不是 `_`：这个开关要留痕（`faq.cache.toggle`），
       操作人由 `ctx` 提供，不接受客户端传参（红线 1）。
    """
    item: FaqItemDTO = await faq_service.set_cache_enabled(db, faq_id, enabled, ctx)
    return ok(FaqCacheDTO(faq_id=item.id, cache_enabled=item.cache_enabled).model_dump())


# ═══════════════════════════════════════════════════════════════
# 运营效能摘要（投影 be-07，不写第二条聚合）
# ═══════════════════════════════════════════════════════════════


@router.get("/stats/overview", summary="运营效能指标摘要")
async def stats_overview(
    start_time: str = Query("", description="统计起始时间"),
    end_time: str = Query("", description="统计结束时间"),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("faq:candidate:list")),
):
    """返回提问量、独立提问人数、FAQ 缓存命中率、知识库覆盖率与平均问答延时。

    🔴 **5 个字段全部投影自 `stats_service.get_overview()`，本端点不写第二条聚合 SQL。**
       两处各写一份必然出现「治理中心的覆盖率 0.82、看板的 0.79」这种
       **双方都自洽、都不报错**的裂开，而排查者不知道该信哪个。
       见 `P3-14` §4.2 / 字典 §9.2 #140。

    ⚠️ `start_time` / `end_time` 同 `mine` 的处理：不传即用默认窗口，
       传了就报 1001（宁可报错也不要静默忽略）。
    """
    if start_time or end_time:
        raise ParamError("本端点不支持自定义时间窗，请留空（走 STATS_DEFAULT_RANGE_DAYS）")

    # `DashboardQuery()` 走全默认值 ⇒ 窗口 = 最近 `STATS_DEFAULT_RANGE_DAYS` 天，
    # 粒度由 be-07 的 `_pick_model` 自己挑。**不在这里传窗口**：
    # 传了就等于本端点有了第二套窗口口径，而「治理中心的数」与「看板的数」
    # 必须来自同一个函数（见 docstring）。
    overview = await stats_service.get_overview(db, ctx, DashboardQuery())
    return ok(
        MiningOverviewDTO(
            question_count=overview.pv,
            unique_user_count=overview.uv,
            faq_hit_rate=overview.faq_hit_rate,
            kb_coverage=overview.coverage_rate,
            avg_latency_ms=overview.avg_latency_ms,
        ).model_dump()
    )


# ═══════════════════════════════════════════════════════════════
# 知识缺口（be-09）
# ═══════════════════════════════════════════════════════════════


@router.get("/gaps", summary="知识缺口诊断清单")
async def list_knowledge_gaps(
    query: GapQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("gap:list")),
):
    """返回未命中或低置信度的提问、提问部门、近期频次、最高相似度与建议分类。

    🔴 **读端点也挂权限码**（`gap:list`）。`knowledge_gap.question` 是**提问原文**，
       而聚合是**跨部门**的（同一问题在不同部门各成一条），列表一次返回全部。
       只要求登录 ⇒ `staff` 能读到别的部门在问什么（红线 4）。
       而 `constants/nav.ts` 的侧栏**不按权限过滤菜单**，所以 `staff` 真的会
       打开治理中心页面并发起这个请求 —— 必须在后端拦，不能靠前端隐藏按钮（红线 7）。

    ⚠️ **刻意不按调用者部门过滤**（be-09 spec §5.8）：能挂这个码的是跨部门的治理
       角色，按部门过滤会让他们的清单只剩自己那一格。代价（`kb_admin` 能读全公司
       各部的提问原文）与 `faq:candidate:list` **是同一取舍**，PRD §11.1 已认领。
    """
    rows, total = await gap.list_gaps(
        db,
        min_frequency=query.min_frequency,
        page=query.page,
        page_size=query.page_size,
    )
    items = [
        KnowledgeGapItemDTO(
            id=row.id,
            question=row.question,
            department_id=row.department_id,
            frequency=row.frequency,
            max_score=row.max_score,
            suggested_category=row.suggested_category,
            status=row.status,
            last_seen_at=row.last_seen_at,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return ok(
        page_result(
            [item.model_dump() for item in items],
            total=total,
            page=query.page,
            page_size=query.page_size,
        )
    )


@router.post("/gaps/{gap_id}/task", summary="缺口一键转建知识补充任务")
async def create_gap_task(
    gap_id: str,
    category: str = Query("", description="建议创建分类"),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("gap:task:create")),
):
    """将知识缺口转为知识补充任务，闭环指导文档补全。

    ## 为什么 `category` 用 `""` 表示「没传」

    壳子的签名就是这样（`Query("")`），而 `category` 的业务含义是
    「人工覆盖模型建议的分类」。`""` 与「真传了一个分类」必须区分开 ——
    否则「用户没填」会变成「把 `suggested_category` 清空」，
    而清空一个已有建议**不会有任何报错**。

    ## 操作人只从 JWT 来（红线 1）

    `ctx.user_id` 是审计与任务的 `uploaded_by` 的唯一来源。
    本端点的 query / path 里**没有任何身份字段** —— 这是刻意的（PRD §11.3 R3）。
    """
    result = await gap.convert_gap_to_task(
        db, gap_id=gap_id, category=category or None, operator=ctx
    )
    return ok(result.model_dump())
