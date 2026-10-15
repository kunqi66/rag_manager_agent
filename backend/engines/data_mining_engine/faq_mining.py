"""FAQ 沉淀挖掘编排（be-08）。

一轮挖掘的六个步骤（be-08 §4.2）：

```
① 取窗口内的 qa_log（faq_hit = false）
② 读它们的 question_embedding → 矩阵 V
③ build_clusters(V, FAQ_CLUSTER_SIMILARITY)          # 并查集，分块矩阵乘
④ 每簇算 frequency / avg_sim / confidence，滤掉 < FAQ_MIN_FREQUENCY
⑤ 去掉与「已有 pending 候选 / 已发布 FAQ」撞车的簇
⑥ 写 faq_candidate(status=pending)
```

🔴 **本模块不 import FastAPI**（分层铁律）。它是 worker 侧与 API 侧共用的编排。

🔴 **LLM 只用在「合并多条问法成一条标准问题与答案」这一步，且失败即降级**：
   一轮挖掘的价值是**候选**，不是那句推荐答案。因为 DeepSeek 抖动就让整轮挖掘
   抛异常，代价是「当天一个候选都产不出来」—— 收益只是报了个错。
   降级用的是簇内成员最高相似度那条的原文（见 `_fallback_answer`）。

⚠️ **`db` 由调用方传进来**（不在这里自建 session）：API 侧用请求的 session、
   worker 侧用 `fresh_session()`（CLAUDE.md：异步 engine 不能跨事件循环复用，
   任务必须自建）。在这里自建的话，API 侧就没法把挖掘纳入同一个事务边界了。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.clients import llm_client
from backend.core.exceptions import ConflictError, NotFoundError, ParamError
from backend.core.ids import new_id
from backend.core.settings import settings
from backend.engines.data_mining_engine.similarity import (
    build_clusters,
    build_matrix,
    l2_normalize,
    nearest_existing,
    representative_index,
)
from backend.models.faq import Faq, FaqCandidate
from backend.models.kb_chunk import KbChunk
from backend.models.qa_log import QaLog, pack_embedding
from backend.schemas.auth import AuthContext
from backend.service import audit_service

logger = logging.getLogger(__name__)

__all__ = [
    "list_candidates",
    "mine_faq_clusters",
    "review_candidate",
    "run_scheduled_mining",
]

#: 一轮挖掘最多扫多少条 `qa_log`。见 `mine_faq_clusters` 的说明。
_MAX_LOGS = 5000

#: LLM 合并问法的 Prompt。要求「不要编」是核心 —— 这一路没有知识正文可依据
#: （FAQ 沉淀不问知识库，只归纳用户怎么问），让它自由发挥就会产出
#: 一条读起来很合理、但没有任何来源支撑的标准答案，然后被发布出去。
_MERGE_SYSTEM_PROMPT = (
    "你是企业知识库的运营助手。用户会给你同一类问题的多种问法。"
    "请归纳出一条标准问法和一条标准答案。"
    "必须遵守：\n"
    "1. 只依据给定的问法归纳，不要引入任何外部知识、不要编造事实、不要给出具体数值；\n"
    "2. 答案若涉及具体业务口径，写成「该问题需要知识库补充说明」这类诚实的占位，"
    "而不是猜测；\n"
    "3. 严格按两行输出：第一行以 `Q:` 开头，第二行以 `A:` 开头，不要其它内容。"
)


def _window_start(now: datetime) -> datetime:
    return now - timedelta(days=settings.mining_lookback_days)


async def _load_window_logs(
    db: AsyncSession, start: datetime, end: datetime, limit: int
) -> list[QaLog]:
    """取窗口内**未命中 FAQ**的提问。

    🔴 `faq_hit = false` 这个条件不能少：命中了 FAQ 的提问说明它**已经沉淀过了**，
       再聚一次只会产出「已发布 FAQ」的重复候选 —— 而且因为
       `faq_hit` 的挖掘窗口与原 FAQ 的内容高度相似，这些重复候选的
       confidence 还特别高，会排在人工审核列表最前面，把真正的新问题挤下去。

    ⚠️ `ORDER BY created_at` 是**为了可复现**：同一窗口跑两次，
       若返回顺序由数据库决定，`build_clusters` 里的簇代表元
       （距质心最近那条）就会漂移，表现为「同一批数据跑两次，候选的标题不一样」。
       顺序固定后，同一批数据跑两次产出完全一致的候选。

    ⚠️ `LIMIT` 而不是全量：窗口内如果有几十万条日志，
       O(N²) 的聚类会跑成小时级。取**最近**的 N 条（`created_at DESC` 后再反转），
       宁可少挖一点也不要让任务跑不完 —— worker 任务超时的表现是
       「beat 每天触发，但一个候选都没有」，比少挖难查得多。
    """
    stmt = (
        select(QaLog)
        .where(
            QaLog.created_at >= start,
            QaLog.created_at < end,
            QaLog.faq_hit.is_(False),
            # 向量为空的行（embedding 调用失败）必须滤掉：它们无法参与聚类，
            # 传进去只会在 `build_matrix` 里被逐条跳过并刷 warning。
            QaLog.question_embedding.is_not(None),
        )
        .order_by(QaLog.created_at.desc(), QaLog.id.desc())
        .limit(limit)
    )
    rows = list((await db.scalars(stmt)).all())
    # 反转为升序，与 `ORDER BY created_at` 的语义一致
    rows.reverse()
    return rows


async def _load_dedup_pool(db: AsyncSession) -> tuple[np.ndarray, list[str]]:
    """去重池 = **已有 pending 候选** ∪ **已发布 FAQ** 的向量。

    两个都要，理由不同：

    - **pending 候选**：防「今天挖一次、明天又挖一次，同一条候选在列表里出现两遍」。
      运营每次看到两条一模一样的候选，会以为系统有问题。
    - **已发布 FAQ**：防「已经发布过了，下周窗口里的同类提问再聚一次又冒出来」。
      只滤 `faq_hit = false` 挡不住它 —— 发布的 FAQ 可能一直没被问到过，
      而窗口里的老提问（发布前产生的）仍在窗口内。

    返回 `(矩阵, 来源 id 列表)`。矩阵为空时是 `(0, 768)` 的空数组，
    `nearest_existing` 对空池返回 `None`（不是异常）。
    """
    blobs: list[bytes | None] = []
    labels: list[str] = []

    candidate_stmt = select(FaqCandidate.question).where(FaqCandidate.status == "pending")
    # 候选表**没有自己的向量列**（字典 §2.4 未定义该列），所以不能直接读。
    # 也不能在这里现算 embedding：每轮要给所有 pending 候选各发一次 HTTP 调用。
    # 做法是拿候选的 `question` 文本回 `qa_log` 找**同文本**那条的向量复用 ——
    # 候选的 question 要么是簇代表元的原文（一定能在 `qa_log` 里找到），
    # 要么是 LLM 归纳的新表述（找不到）。找不到就跳过该候选：
    # 下一轮它还在，不会漏，只是这一轮的去重池少一条。
    candidate_questions = list((await db.scalars(candidate_stmt)).all())
    if candidate_questions:
        reuse_stmt = select(QaLog.question, QaLog.question_embedding).where(
            QaLog.question.in_(candidate_questions),
            QaLog.question_embedding.is_not(None),
        )
        for question, blob in (await db.execute(reuse_stmt)).all():
            blobs.append(blob)
            labels.append(f"candidate:{question}")

    faq_stmt = select(Faq.id, Faq.question_embedding).where(
        Faq.question_embedding.is_not(None)
    )
    for faq_id, blob in (await db.execute(faq_stmt)).all():
        blobs.append(blob)
        labels.append(f"faq:{faq_id}")

    matrix, kept = build_matrix(blobs)
    return matrix, [labels[index] for index in kept]


def _fallback_answer(logs: list[QaLog], members: list[int]) -> str:
    """LLM 不可用时的降级答案。

    用簇内**第一条**成员的原文。为什么不是「最长的一条」：长度与「更像标准答案」
    没有关系，而「第一条」至少是确定性的（`_load_window_logs` 已固定顺序），
    不会让降级产出的内容每次都不一样。

    前缀刻意写明「未经归纳」，避免它被误当成已核定的标准答案直接发布。
    """
    first = logs[members[0]]
    return f"（自动归纳不可用，以下为原始提问原文，请人工完善）\n{first.question}"


async def _merge_questions(logs: list[QaLog], members: list[int]) -> tuple[str, str]:
    """把一簇问法合并成 `(标准问题, 推荐答案)`。**LLM 失败即降级，不抛。**

    为什么要合并而不是直接取代表元：同一簇里「报销流程是啥」「怎么走报销」
    「报销怎么弄」三句话，直接取其中一句当标准问题，另外两种问法就永远匹配不上 ——
    LLM 归纳出的表述通常更完整。

    ⚠️ **LLM 的产出要过长度校验**：模型可能返回一整段解释、
       或不按 `Q:`/`A:` 格式输出。校验不过就降级 ——
       把一段没有结构的长文本当成 `question` 存进去，
       候选列表上会出现一条标题是几百字的卡片。
    """
    # 去重后最多送 20 条，避免长上下文既慢又贵
    seen: list[str] = []
    for index in members:
        text = (logs[index].question or "").strip()
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= 20:
            break
    if not seen:
        return "", ""

    try:
        reply = await llm_client.chat(
            [
                {"role": "system", "content": _MERGE_SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(f"- {text}" for text in seen)},
            ],
            # 归纳任务要稳不要发挥。比问答的 0.3 再低一档。
            temperature=0.1,
        )
    except Exception:  # noqa: BLE001 —— 见模块头部：LLM 失败必须降级
        logger.exception("候选问答合并失败，降级为原始提问")
        return "", ""

    question = ""
    answer = ""
    for line in reply.text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Q:") and not question:
            question = stripped[2:].strip()
        elif stripped.startswith("A:") and not answer:
            answer = stripped[2:].strip()

    # 长度上限对齐 `FaqReviewPayload` 的 `max_length`（1000 / 8000）——
    # 超了的话运营点「发布」会吃一个 422，而候选本身是合法落库的，
    # 表现为「这条候选永远发不出去」。在这里截断比在审核时报错好。
    if not question or not answer or len(question) > 1000 or len(answer) > 8000:
        logger.warning("候选问答合并结果不合规（question=%d answer=%d），降级", len(question), len(answer))
        return "", ""
    return question, answer


def _confidence(avg_sim: float, frequency: int) -> float:
    """`avg_sim × min(1, frequency / FAQ_CONFIDENCE_FREQ_REF)`（be-08 §3.1）。

    频次项**归一化后封顶在 1**：不封顶的话，一个被反复问的寒暄句
    （「你好」被问 200 次）会靠频次盖过真正该沉淀的高相似度簇，
    而这个数排在前面的唯一用途就是让人先看它。
    """
    freq_ref = max(1, settings.faq_confidence_freq_ref)
    ratio = min(1.0, frequency / freq_ref)
    return float(max(0.0, min(1.0, avg_sim * ratio)))


async def _resolve_document_ids(db: AsyncSession, logs: list[QaLog], members: list[int]) -> list[str]:
    """簇成员的 `qa_log.allowed_ids`（**chunk** id）→ **document** id 去重集。

    🔴 必须反查。`allowed_ids` 里存的是 chunk id（be-05 §10-19 冻结），
       而候选卡片要显示的是「关联知识单元」= 文档。直接把 chunk id 存进
       `related_doc_ids`，前端的标题查询永远查不到东西，而列表**有值**、
       看起来完全正常 —— 这是本模块最容易静默出错的一处（be-07 的
       `top_documents` 踩过同款，见 P2-13 §10.1 #129）。

    用一条 `IN` 查询而不是逐个查：簇成员可能上百，逐个查就是 N+1。
    """
    chunk_ids: list[str] = []
    for index in members:
        for chunk_id in logs[index].allowed_ids or []:
            # `dict.fromkeys` 去重且保序
            if chunk_id not in chunk_ids:
                chunk_ids.append(chunk_id)
    if not chunk_ids:
        return []
    stmt = select(KbChunk.document_id).where(KbChunk.id.in_(chunk_ids))
    return list(dict.fromkeys((await db.scalars(stmt)).all()))


def _cluster_key(logs: list[QaLog], members: list[int]) -> str:
    """`md5("|".join(sorted(成员 qa_log.id)))[:16]`。

    用 `id` 而不是 `question` 文本：文本可能含空白差异，且长文本做键不好索引。
    `sorted` 保证与簇内成员顺序无关 —— 顺序变了键还一样，
    这样「同一天重复跑」能靠它认出同一簇（虽然跨天去重靠的是向量比对）。
    """
    ids = sorted(logs[index].id for index in members)
    return hashlib.md5("|".join(ids).encode("utf-8")).hexdigest()[:16]


async def mine_faq_clusters(
    db: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    min_frequency: int | None = None,
    max_logs: int = _MAX_LOGS,
) -> dict[str, object]:
    """跑一轮聚类挖掘，返回统计字典（对应 `MineResultDTO`）。

    Args:
        start / end: 挖掘窗口。默认 `[now - MINING_LOOKBACK_DAYS, now)`。
        min_frequency: 成簇的最小频次，默认走 `FAQ_MIN_FREQUENCY`。
        max_logs: 单轮扫描上限，见 `_load_window_logs`。

    🔴 **幂等性靠「去重池」而不是「唯一键」**：同一批数据跑两次，
       第二次的每个簇都会在去重池里找到自己（第一次刚写进去的 pending 候选），
       于是 `candidates_created = 0`。这是**刻意的设计**，不是巧合 ——
       用 `cluster_key` 做唯一约束挡不住重复：同一问题今天聚出簇 A、
       明天簇 B（窗口滑动后成员变了），键天然不同。
    """
    now = end or datetime.now()
    window_start = start or _window_start(now)
    if window_start >= now:
        # 参数颠倒会让窗口为空 ⇒ 永远挖不出东西，且不报错。
        # 这类「配置/参数写反」必须报出来，否则表现为「挖掘功能坏了」。
        raise ParamError("挖掘窗口的起始时间必须早于结束时间")

    threshold = settings.faq_cluster_similarity
    threshold_min = min_frequency if min_frequency is not None else settings.faq_min_frequency
    if threshold_min < 1:
        raise ParamError("min_frequency 必须 ≥ 1")

    logs = await _load_window_logs(db, window_start, now, max_logs)
    if not logs:
        logger.info("挖掘窗口内没有待聚类的提问，窗口 %s ~ %s", window_start, now)
        return {
            "candidates_created": 0,
            "clusters_scanned": 0,
            "window_start": window_start,
            "window_end": now,
            "partial": False,
        }

    vectors, kept = build_matrix([log.question_embedding for log in logs])
    # 只保留向量合规的行。`build_matrix` 已把脏行滤掉，这里把 `logs`
    # 同步裁剪成同一套下标 —— 否则 `members` 里的下标会指到别的行，
    # 候选的「代表性问题」就安到别人头上了（且看起来完全正常）。
    logs = [logs[index] for index in kept]
    if not logs:
        logger.warning("窗口内 %d 条提问的向量全部不合规，本轮跳过", len(kept))
        return {
            "candidates_created": 0,
            "clusters_scanned": 0,
            "window_start": window_start,
            "window_end": now,
            "partial": False,
        }

    clusters = build_clusters(vectors, threshold)
    pool, pool_labels = await _load_dedup_pool(db)

    created = 0
    for members in clusters:
        frequency = len(members)
        if frequency < threshold_min:
            continue

        representative = representative_index(vectors, members)
        # 簇内平均相似度：成员与质心的平均余弦。已归一化 ⇒ 点积即余弦。
        centroid = l2_normalize(vectors[list(members)].mean(axis=0, keepdims=True))[0]
        avg_sim = float(np.mean(vectors[list(members)] @ centroid))
        confidence = _confidence(avg_sim, frequency)

        # ---- ⑤ 去重：与已有 pending 候选 / 已发布 FAQ 撞车则跳过 ----
        hit = nearest_existing(vectors[representative], pool, threshold)
        if hit is not None:
            logger.info(
                "候选与已有条目撞车（相似度 %.3f，来源 %s），跳过",
                hit[1],
                pool_labels[hit[0]] if hit[0] < len(pool_labels) else "?",
            )
            continue

        question, answer = await _merge_questions(logs, members)
        if not question:
            # 降级：用代表元原文当问题，原文当答案
            question = logs[representative].question.strip()
            answer = _fallback_answer(logs, members)
        if not question:
            continue

        doc_ids = await _resolve_document_ids(db, logs, members)
        db.add(
            FaqCandidate(
                id=new_id("faq_c"),
                cluster_key=_cluster_key(logs, members),
                question=question,
                answer=answer,
                frequency=frequency,
                confidence=confidence,
                related_doc_ids=doc_ids,
                status="pending",
            )
        )
        created += 1
        # 把新候选也放进池子：同一轮里两个簇的向量若高度相似，
        # 不放进池子的话它们会各自成为一条候选 —— 同一轮内自我撞车。
        pool = np.vstack([pool, vectors[representative]]) if pool.size else vectors[[representative]]
        pool_labels.append(f"candidate:{question}")

    if created:
        await db.commit()

    logger.info(
        "挖掘完成：窗口 %s ~ %s，扫过 %d 条提问 / %d 个簇，新增 %d 条候选",
        window_start,
        now,
        len(logs),
        len(clusters),
        created,
    )
    return {
        "candidates_created": created,
        "clusters_scanned": len(clusters),
        "window_start": window_start,
        "window_end": now,
        "partial": len(logs) >= max_logs,
    }


async def run_scheduled_mining(db: AsyncSession) -> dict[str, object]:
    """beat 每日调用的入口。**窗口是「上次挖掘位点」的近似**。

    ⚠️ 「上次挖掘位点」没有单独存表（字典 §2.4 没有这个字段，也不该新增）——
       用 `MINING_LOOKBACK_DAYS` 的滑动窗口代替。代价是**重复扫描**：
       窗口重叠部分每天都会被扫，靠去重池挡掉重复候选。
       这是刻意的取舍：加一张位点表要处理「任务失败到位点已推进」的一致性问题，
       而重复扫描的代价只是多算几次相似度。
    """
    return await mine_faq_clusters(db)


async def review_candidate(
    db: AsyncSession, candidate_id: str, action: str, question: str, answer: str, ctx: AuthContext
) -> tuple[str, str | None]:
    """审核一条候选。返回 `(最终状态, 新 FAQ id 或 None)`。

    状态机（be-08 §4.3）：

    - `pending` → `publish`：写一行 `faq` + 候选置 `published`
    - `pending` → `reject`：候选置 `rejected`
    - **已审过的候选再审一次 ⇒ `ConflictError`（3002）**

    🔴 **重复审核必须报错而不是幂等返回**：`publish` 不是幂等操作 ——
       两次发布会产生两条 FAQ。运营双击按钮、或网络重试都会走到这里，
       幂等返回意味着「第二次点击悄悄改写了已发布的内容」，
       而第一条 FAQ 已经进了索引、可能已经被答出去了。

    🔴 **`question`/`answer` 以请求体为准，不做空值兜底**：用户清空答案后
       若我们「回退到原推荐答案」，会发布出一条用户明确想改掉的内容
       （fe-06 §3 已定）。

    🔴 **形参是 `ctx` 而不是 `reviewer_id`**（be-06 §5.4 + 红线 1）：
       两个理由。① 审核人**只能**来自 JWT，把 `AuthContext` 整个传进来，
          身份就与审计行里的 `user_id` / `ip` 同源 —— 传一个裸 id 的话，
          「谁审的」在 `faq_candidate.reviewer_id` 与审计行里是两份可以
          各自写错的数据；② `audit_service.record()` 需要 `ctx.ip`，
          而引擎层拿不到 `Request`（`ip` 放在 `AuthContext` 里就是为了这件事）。
    """
    if action not in {"publish", "reject"}:
        # 理论上 pydantic 的 Literal 已挡住，但本函数也由 worker/脚本调用，
        # 不能假设调用方一定过了 DTO 校验。
        raise ParamError("action 只能是 publish 或 reject")

    candidate = await db.get(FaqCandidate, candidate_id)
    if candidate is None:
        raise NotFoundError("候选 FAQ 不存在")
    if candidate.status != "pending":
        raise ConflictError(f"该候选已处于 {candidate.status} 状态，不能重复审核")

    now = datetime.now()
    reviewer_id = ctx.user_id
    faq_id: str | None = None

    if action == "reject":
        candidate.status = "rejected"
        candidate.reviewer_id = reviewer_id
        candidate.reviewed_at = now
        # 留痕用 `record()`（搭本次审核的事务），**不是** `record_detached()`：
        # 这里有一条真实的业务事务可搭，而「驳回」与「留下一行驳回记录」
        # 应当同生共死 —— 用 detached 的话，驳回成功了而留痕失败会变成
        # 「候选变 rejected 了，但没人知道是谁在什么时候驳的」。
        #
        # ⚠️ 键集里虽然有 `reason`，但 `FaqReviewPayload` **没有**这个字段
        #    （前端 fe-06 的审核弹窗不收集驳回理由），所以这里只记
        #    `candidate_id`。白名单是**上限**不是必填集 —— 留着 `reason`
        #    是为了将来前端补上理由时不必再动 audit_service。
        audit_service.record(
            db,
            ctx,
            audit_service.AuditAction.FAQ_REJECT,
            target_type=audit_service.AuditTargetType.FAQ,
            target_id=candidate_id,
            detail={"candidate_id": candidate_id},
        )
        await db.commit()
        return "rejected", None

    # ---- 发布 ----
    # 向量必须现算：候选表没有存向量（字典 §2.4 未定义该列），
    # 而 `faq.question_embedding` 是索引的唯一数据来源。
    #
    # 🔴 **必须走 `embed_query`（带 BGE 查询前缀），不能用裸 `embed_texts`。**
    #    这个向量**唯一的用途就是被用户提问去比**（两处消费方都是查询侧语义）：
    #      ① `faq_cache.search()` —— 拿 `query_dense` 比它；
    #      ② 本模块的「已发布 FAQ 不重复成候选」判重 —— 拿 `qa_log.question_embedding` 比它。
    #    而这两处的向量都出自 `retrieval.embed_query`，是**带前缀**的。
    #    用不带前缀的 passage 口径去编码，等于两侧口径不一致。
    #
    #    ⚠️ 后果实测（2026-10-15，本机 BGE）：同一条问题「带前缀 vs 不带前缀」
    #       余弦只有 **0.827 / 0.942 / 0.837**，而 `faq_match_threshold = 0.92`
    #       ⇒ **用户一字不差地问同一条已发布 FAQ，也会匹配失败**（两条里有一条中招）。
    #       症状是「启用缓存后还是走 LLM」，慢、费 token，**但不报任何错** ——
    #       与「缓存没启用」在界面上完全看不出区别。跨问题对照只有 0.18~0.28，
    #       所以 0.83 这个分数看着"挺高"，很容易被当成正常命中。
    from backend.engines.search_engine import retrieval

    vector: list[float] | None = None
    try:
        vector = await retrieval.embed_query(question)
    except Exception:  # noqa: BLE001 —— 见下
        # ⚠️ **发布不能因为向量算不出来而失败**：FAQ 已经被人审过、
        #    内容本身是对的，只是暂时进不了毫秒级缓存。让发布成功、
        #    索引少一条，比让运营的审核成果丢掉好得多。
        #    代价是这条 FAQ 走正常检索路径 —— 功能降级，不是故障。
        logger.exception("发布 FAQ 时计算向量失败（faq_id 待生成，候选 %s）", candidate_id)

    faq_id = new_id("faq")
    db.add(
        Faq(
            id=faq_id,
            question=question,
            answer=answer,
            question_embedding=pack_embedding(vector) if vector else None,
            cache_enabled=True,
            source_candidate_id=candidate.id,
            published_by=reviewer_id,
            published_at=now,
            hit_count=0,
        )
    )
    candidate.status = "published"
    candidate.reviewer_id = reviewer_id
    candidate.reviewed_at = now
    # 候选的问答也更新成审定后的版本：列表上要显示「当时审的是什么」，
    # 留着 LLM 的原始推荐会让「已发布」的候选与 FAQ 内容对不上。
    candidate.question = question
    candidate.answer = answer
    # 同上：搭本次发布的事务。`faq_id` 已在上面生成，所以能直接进 detail。
    # ⚠️ **`target_id` 取 `faq_id` 而不是 `candidate_id`**：发布这个动作的结果是
    #    「多了一条 FAQ」，后续 `faq.cache.toggle` 等动作的 `target_id` 也是
    #    `faq_id` —— 同一个目标上的动作要能按 `(target_type, target_id)` 串起来。
    #    `candidate_id` 放进 detail，两者的对应关系仍然可查。
    audit_service.record(
        db,
        ctx,
        audit_service.AuditAction.FAQ_PUBLISH,
        target_type=audit_service.AuditTargetType.FAQ,
        target_id=faq_id,
        detail={
            "candidate_id": candidate_id,
            "faq_id": faq_id,
            "frequency": candidate.frequency,
        },
    )
    await db.commit()

    # 索引刷新**在 commit 之后**：先刷索引再提交的话，提交失败会留下
    # 「索引里有、库里没有」的幽灵 FAQ —— 它能被命中并答出去，
    # 而任何地方都查不到它。
    from backend.service import faq_service

    await faq_service.publish_change("upsert", faq_id)
    return "published", faq_id


async def list_candidates(
    db: AsyncSession,
    *,
    status: str,
    min_confidence: float,
    page: int,
    page_size: int,
) -> tuple[list[FaqCandidate], int]:
    """候选分页。返回 `(当页 ORM 行, 总数)`。

    ⚠️ 本函数返回 **ORM 行**而不是 DTO：`related_docs` 的权限回填需要
       `AuthContext`，那属于 service 层（`faq_service.attach_related_docs`）。
       引擎层不碰权限判定 —— 它只产出数据。
    """
    from sqlalchemy import func

    conditions = [FaqCandidate.status == status]
    if min_confidence > 0:
        conditions.append(FaqCandidate.confidence >= min_confidence)

    total = await db.scalar(
        select(func.count()).select_from(FaqCandidate).where(*conditions)
    ) or 0
    stmt = (
        select(FaqCandidate)
        .where(*conditions)
        # `confidence` 降序 = 最该处理的排最前，这是运营看这个列表的唯一理由。
        # 次级键 `id` 保证翻页稳定（同 confidence 时否则可能换位）。
        .order_by(FaqCandidate.confidence.desc(), FaqCandidate.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list((await db.scalars(stmt)).all()), int(total)
