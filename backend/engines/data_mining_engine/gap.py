"""知识缺口闭环引擎（be-09）。

链路（PRD §6.3.3）：

```
未命中提问（be-05 已落 qa_log）
  → aggregate_gaps：聚合成 knowledge_gap(open)，累加 frequency      ← 本模块
  → convert_gap_to_task：转建 kb_import_task(supplement) + 置 tasked ← 本模块 + be-04
  → 补料入库并配权限（be-04）
  → 同一问题再问时命中（be-05 落 qa_log）
  → 下一轮 aggregate_gaps 扫到 → closed                            ← 闭环收口
```

🔴 **本模块不碰 `kb_import_task` 的表**：建任务走 be-04 的 `create_supplement_task`。
   直接 `INSERT` 会让「补充任务」这个语义有两份实现，而两份的不一致
   （比如一边写 `batch_id` 一边不写）不会有任何报错。

🔴 **`engines/` 禁止 import FastAPI**（CLAUDE.md 分层铁律）。本模块只依赖
   `models/` / `schemas/` / `core/` / be-04 与 be-06 的门面。

---

## 三处最容易写错的地方（都已在下面就地注明）

1. **`is_miss` 不能只看 PRD §6.3.3 的字面** —— 会把「FAQ 已作答」与
   「检索到但全被权限挡住」误判成缺口，两者都**静默**污染缺口池。
2. **聚合必须按 `created_at` 升序、且新建的缺口要立刻进内存匹配集** ——
   少了任一条，同一问题会建出两条缺口或静默少计。
3. **幂等靠缺口自己的 `last_seen_at`** —— 没有它，beat 每 10 分钟 × 窗口 3 小时
   会把 `frequency` 累加到 18 倍，而「虚高」不会触发任何告警。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.clients import llm_client
from backend.core.exceptions import ConflictError, DependencyError, NotFoundError
from backend.core.ids import new_id
from backend.core.settings import settings
from backend.engines.data_mining_engine import similarity
from backend.engines.import_engine import create_supplement_task
from backend.models.audit_log import SYSTEM_USER_ID
from backend.models.kb_import_task import KbImportTask
from backend.models.knowledge_gap import (
    ACTIVE_STATUSES,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_TASKED,
    KnowledgeGap,
)
from backend.models.qa_log import QaLog
from backend.schemas.auth import AuthContext
from backend.schemas.mining import GapTaskResult
from backend.service import audit_service

logger = logging.getLogger(__name__)

__all__ = [
    "AggregateResult",
    "aggregate_gaps",
    "convert_gap_to_task",
    "is_miss",
    "list_gaps",
    "suggest_category",
]

#: 缺口 id 前缀。形态由 `core/ids.py` 统一（`gap_12位hex`）。
_GAP_PREFIX = "gap"

#: 单轮聚合最多把多少条未命中提问读进内存。**不是性能优化，是防呆**：
#: 窗口算错（比如传了个 `since=1970`）时，没有它就会把全表读进内存然后 OOM；
#: 有了它，最坏情况只是「这一轮少聚合了一部分」，而下一轮会补上（窗口是滑动的）。
_MAX_SCAN = 5000

#: `suggest_category` 的 Prompt。**要求「只回一个词」并显式禁止解释** ——
#: 不禁止的话 DeepSeek 会回「这个问题属于：人力资源 / 薪酬福利」这种整句，
#: 而 `suggested_category` 是 `VARCHAR(64)`，超长会被 MySQL 截断（或报错）。
_SUGGEST_PROMPT = (
    "你是企业知识库的分类助手。请为企业知识库中的一个分类名称，"
    "给下面这个员工提问推荐一个最贴切的分类。\n"
    "要求：只输出分类名称本身，不要标点、不要解释、不要引号，不超过 8 个字。\n"
    "提问：{question}"
)

#: 自动关闭时的**操作人**。be-06 已把 `SYSTEM_USER_ID = "system"` 定为系统动作的
#: 唯一 user_id（`models/audit_log.py:23`）—— 不新造「auto」之类的字面量，
#: 否则审计里会出现两个都表示「系统」的 id，按 user_id 过滤时必漏一半。
_SYSTEM_CTX = AuthContext(
    user_id=SYSTEM_USER_ID,
    username=SYSTEM_USER_ID,
    department_id=None,
    depts=[],
    roles=[],
    perms=[],
)


@dataclass(slots=True)
class AggregateResult:
    """一轮聚合的结果。字段名与任务返回的 dict 一一对应（便于核对 A8）。"""

    #: 窗口内扫到的「未命中候选行」数（已过 `faq_hit` 与向量非空两道筛）
    scanned: int = 0
    #: 新建的缺口数
    created: int = 0
    #: 被累加的缺口数（同一缺口累加多次只算 1）
    updated: int = 0
    #: 自动关闭的缺口数
    closed: int = 0
    #: 🔴 **被权限挡下而排除的行数**。它是「某部门权限配少了」的强信号，
    #: 不计入缺口池但必须能看见 —— 否则「昨天少了 30 条未命中」无人能解释。
    skipped_by_acl: int = 0
    #: 窗口，回带便于核对「跑的是哪一段」
    window: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "created": self.created,
            "updated": self.updated,
            "closed": self.closed,
            "skipped_by_acl": self.skipped_by_acl,
            "window": self.window,
        }


def is_miss(
    *,
    faq_hit: bool,
    recalled_ids: list[str] | None,
    allowed_ids: list[str] | None,
    top_score: float | None,
) -> bool:
    """未命中判定（纯函数，便于单测）。见 be-09 模块设计 §4.2。

    PRD §6.3.3 的字面是「`allowed_chunks` 为空 **或** 最高相似度 < `KB_MIN_SCORE`」，
    但**照字面实现是错的** —— `allowed_ids` 在 be-05 落库时是「**后置复核准入**」，
    于是有两个不是知识缺口的场景会命中第一个条件，且都不报错：

    ① **FAQ 缓存直接作答的提问**：该路径根本没检索，`allowed_ids` 天然为空
       ⇒ 每一条被 FAQ 答上的提问都会进缺口池，而 FAQ 恰恰最不可能是知识缺口。
       故 `faq_hit=True` 必须先短路掉（聚合 SQL 里的 `faq_hit = false` 是同一条防线）。
    ② **检索到了、但候选全被权限挡住**：知识**存在**，缺的是权限。
       判成缺口会让运营去补一份已经存在的材料，并把「本部门不该看到的话题」
       以提问文本的形式堆进治理页。故 `recalled_ids` 非空而 `allowed_ids` 空 ⇒ **不是缺口**。

    被排除的第 ② 类不丢：调用方计入 `AggregateResult.skipped_by_acl` 并告警。

    ⚠️ 第 ② 条还不能只靠 `top_score` 判 —— `top_score` 在 be-05 里只统计**准入集合**
       （`retrieval.py:268`），所以「准许入的都很低分」与「高分但被挡」在它上面长得一样。
       必须配 `recalled_ids` 才能把这两个维度分开。
    """
    if faq_hit:
        return False
    if not recalled_ids:
        return True
    if not allowed_ids:
        # 有候选、但一条都没通过权限复核 ⇒ 缺权限，不是缺知识。
        return False
    return top_score is not None and top_score < settings.kb_min_score


def _unit_vector(blob: bytes | None) -> np.ndarray | None:
    """单个 BLOB → **已 L2 归一化**的一维向量；不可解析返回 `None`。

    🔴 刻意复用 `similarity.build_matrix([blob])` 而**不是**自己
    `from_blob` + `l2_normalize`：归一化有没有做，直接决定点积是不是余弦。
    漏做的那条路径得到的相似度会**整体偏移**（未归一化时模长越大分越高），
    而阈值看起来「只是有点不准」—— **这个错不会报错，只会让阈值失效**。
    一条路径归一化、另一条不归一化，正是最该避免的分叉，所以这里只有一处实现。
    """
    matrix, kept = similarity.build_matrix([blob])
    if not kept:
        return None
    return matrix[0]


async def suggest_category(question: str) -> str | None:
    """让 DeepSeek 给问题推荐一个分类。**失败返回 `None`，绝不抛。**

    只在**新建缺口**时调一次（累加 `frequency` 时不重复调）—— 缺口是持续累加的实体，
    同一问题被问 30 次只需推荐一次，重复调是纯粹的 LLM 浪费。

    超时单列 `GAP_LLM_SUGGEST_TIMEOUT_SECONDS`（默认 5s，**刻意小于全局超时**）：
    分类推荐是**可选装饰**，一次 DeepSeek 卡顿不该占满整轮聚合的预算。
    ⚠️ 这个值必须**真的传下去**（`timeout_seconds=`）—— 只定义不传等于没有，
    而这不会报错，只会表现为「偶尔一整轮聚合慢了 60 秒」，且没人能解释。
    """
    prompt = _SUGGEST_PROMPT.format(question=question[:500])
    try:
        reply = await llm_client.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=32,
            timeout_seconds=settings.gap_llm_suggest_timeout_seconds,
        )
    except DependencyError as exc:
        # 依赖异常是最可能的一种（超时 / 未配 key / 5xx）。降级留空即可 ——
        # 缺口照常入池，`suggested_category` 为空，运营自己填。
        logger.warning("缺口分类推荐失败，留空：%s", exc)
        return None
    except Exception:  # noqa: BLE001 —— 见 docstring：本函数**绝不**把异常抛给聚合主流程
        logger.exception("缺口分类推荐出现意外异常，留空")
        return None

    text = (reply.content or "").strip().strip("。.、,，\"'“”")
    if not text:
        return None
    # 超长直接判失败而不是截断：截断会得到一个**看似正常**的半个词（如「人力资源与薪」），
    # 它会被当成有效分类一路带到导入中心，而成因（模型没听话）永远查不出来。
    if len(text) > 32:
        logger.warning("缺口分类推荐回了一段话（%d 字），判为失败留空", len(text))
        return None
    return text


# ═══════════════════════════════════════════════════════════════
# 聚合
# ═══════════════════════════════════════════════════════════════


async def _load_miss_rows(
    db: AsyncSession, *, since: datetime, until: datetime
) -> list[QaLog]:
    """窗口内的未命中候选行。**SQL 侧先把大头筛掉，Python 侧才做向量判定**。

    `faq_hit = false` 进 SQL 而不是留给 `is_miss`：FAQ 命中的行在库里可能占多数，
    全捞出来再一条条短路掉是白读。两处判据一致（`is_miss` 的第一条分支是同一个意思）。
    """
    stmt = (
        select(QaLog)
        .where(
            QaLog.faq_hit.is_(False),
            QaLog.question_embedding.is_not(None),
            QaLog.created_at >= since,
            QaLog.created_at < until,
        )
        # 🔴 **必须按 created_at 升序**（be-09 模块设计 §4.2）。
        #    顺序反了会让「同一问题在窗口内的两条提问」中较早的那条
        #    碰到较晚那条刚抬高的水位线，被误判「已计过」而**静默少计**。
        .order_by(QaLog.created_at.asc(), QaLog.id.asc())
        .limit(_MAX_SCAN)
    )
    rows = list((await db.scalars(stmt)).all())
    if len(rows) >= _MAX_SCAN:
        logger.warning(
            "缺口聚合撞到单轮扫描上限 %d 条，本轮只处理前 %d 条（下一轮窗口会补上）",
            _MAX_SCAN,
            _MAX_SCAN,
        )
    return rows


async def _load_active_gaps(db: AsyncSession) -> list[KnowledgeGap]:
    """活跃（`open` / `tasked`）缺口 —— 只有它们参与匹配。"""
    stmt = select(KnowledgeGap).where(KnowledgeGap.status.in_(ACTIVE_STATUSES))
    return list((await db.scalars(stmt)).all())


class _Matcher:
    """「同部门 + 余弦 ≥ 阈值」的最近邻匹配池。

    单独抽一个类是因为它有**两处状态**且都必须随新建缺口一起更新：
    `_vectors` 与 `_members`。写成两个平行 list 再在函数里维护，
    很容易只 append 了一个 —— 而后果是 `argmax` 的下标指到别人身上，
    表现是「频次累加到了一条不相干的缺口上」，极难发现。
    """

    __slots__ = ("_threshold", "_by_dept", "_vectors", "_members")

    def __init__(self, threshold: float) -> None:
        self._threshold = threshold
        #: `department_id -> 该部门的缺口在 `_members` 里的位置列表`
        self._by_dept: dict[str | None, list[int]] = {}
        #: 所有参与匹配的缺口（按加入顺序）
        self._members: list[KnowledgeGap] = []
        #: 与 `_members` **等长等序**的向量矩阵
        self._vectors: np.ndarray = np.empty((0, similarity.EMBED_DIM), dtype=np.float32)

    def add(self, gap: KnowledgeGap, vector: np.ndarray | None) -> None:
        """把一个缺口加进匹配池。

        `vector` 必须是**已 L2 归一化**的（用 `_unit_vector` 取）——
        池子里的向量与查询向量必须同口径，否则点积不是余弦。
        向量为 `None`（脏行）时不加入：它匹配不上任何东西，留着只会让
        `argmax` 有机会把下标指到它身上。
        """
        if vector is None:
            return
        self._by_dept.setdefault(gap.department_id, []).append(len(self._members))
        self._members.append(gap)
        row = vector.reshape(1, -1)
        self._vectors = row if self._vectors.size == 0 else np.vstack([self._vectors, row])

    def extend(self, gaps: list[KnowledgeGap]) -> int:
        """批量加入；返回**实际生效**的条数（脏行不算）。"""
        vectors, kept = similarity.build_matrix(g.question_embedding for g in gaps)
        for row, index in enumerate(kept):
            self.add(gaps[index], vectors[row])
        return len(kept)

    def best(
        self, department_id: str | None, vector: np.ndarray
    ) -> tuple[KnowledgeGap, float] | None:
        """同部门内的最近缺口。返回 `(缺口, 相似度)`；没有够像的返回 `None`。"""
        positions = self._by_dept.get(department_id)
        if not positions:
            return None
        # 只在该部门的子矩阵上算 —— 双键（问题 + 部门）就落在这里。
        # 全量算完再按部门过滤是错的：跨部门的同问题会被判成同一条缺口，
        # 而「部门」这个维度塌掉之后**无法从数据里还原**。
        sub = self._vectors[positions]
        hit = similarity.nearest_existing(vector, sub, self._threshold)
        if hit is None:
            return None
        local, score = hit
        return self._members[positions[local]], score


async def aggregate_gaps(
    db: AsyncSession, *, since: datetime, until: datetime
) -> AggregateResult:
    """把窗口内的未命中提问聚合进 `knowledge_gap`，并自动关闭已命中的 `tasked` 缺口。

    ## 幂等：水位线就是缺口自己的 `last_seen_at`

    朴素的「每轮扫窗口并累加」在本项目的 beat 配置下**必错**：
    beat 每 10 分钟一轮、回溯窗口 3 小时 ⇒ 同一条 `qa_log` 被累加 **18 次**，
    `frequency` 虚高一个数量级，而 A8 断言的是「≈ 30」。

    所以每个缺口自带水位线：**只累加 `created_at > 该缺口.last_seen_at` 的提问**。
    不需要任何额外存储（不引入 Redis 键），且天然按部门/缺口分开。

    **已知代价**：水位线之后**迟到**的日志会被跳过而少计。内部规模下
    beat 10 分钟一轮、窗口 3 小时，迟到 3 小时以上的 `qa_log` 只可能来自 worker 堆积。
    **少计可接受，重复计数不可接受** —— 频次是给人看的决策依据。
    """
    result = AggregateResult(
        window={"since": since.isoformat(), "until": until.isoformat()}
    )

    threshold = settings.faq_cluster_similarity
    rows = await _load_miss_rows(db, since=since, until=until)
    matcher = _Matcher(threshold)
    matcher.extend(await _load_active_gaps(db))

    for row in rows:
        vector = _unit_vector(row.question_embedding)
        if vector is None:
            # 长度不符 / 空向量（字典 §2.4.1 要求「跳过并计数」，不抛）
            logger.warning("缺口聚合跳过 qa_log=%s：question_embedding 不可解析", row.id)
            continue

        top_score = row.top_score
        allowed = row.allowed_ids or []
        if not allowed and row.recalled_ids:
            # 反例 ② 被排除的一类。**不计入缺口池，但要能看见**（见 `is_miss`）。
            result.skipped_by_acl += 1
            continue
        if not is_miss(
            faq_hit=row.faq_hit,
            recalled_ids=row.recalled_ids,
            allowed_ids=allowed,
            top_score=top_score,
        ):
            continue

        result.scanned += 1
        hit = matcher.best(row.department_id, vector)

        if hit is not None:
            gap, _score = hit
            if row.created_at <= gap.last_seen_at:
                # **已计过** —— 这一行就是幂等本身。窗口重叠部分走到这里。
                continue
            gap.frequency += 1
            gap.last_seen_at = row.created_at
            if top_score is not None:
                gap.max_score = (
                    top_score if gap.max_score is None else max(gap.max_score, top_score)
                )
            result.updated += 1
            continue

        # 新建。⚠️ **必须立刻进匹配池**：否则窗口内第 2 条同问题的提问
        #    匹配不到刚建这条，会再建一条 —— 同一问题两条缺口、各 frequency=1。
        #    这与 be-08 的 O(N²) 是同一个「在集合上迭代时集合会变」的坑。
        gap = KnowledgeGap(
            id=new_id(_GAP_PREFIX),
            question=row.question,
            question_embedding=row.question_embedding,
            department_id=row.department_id,
            frequency=1,
            max_score=top_score,
            suggested_category=None,
            status=STATUS_OPEN,
            last_seen_at=row.created_at,
        )
        # 分类推荐：**可选、不阻塞**。只在新建时调一次。
        gap.suggested_category = await suggest_category(row.question)
        db.add(gap)
        matcher.add(gap, vector)
        result.created += 1

    if settings.gap_auto_close_enabled:
        result.closed = await _auto_close(db, until=until, threshold=threshold)

    await db.commit()
    logger.info(
        "缺口聚合完成：窗口 %s ~ %s，扫到 %d 条未命中 → 新建 %d / 累加 %d / 自动关闭 %d "
        "（另有 %d 条因权限被挡下，未计入缺口池）",
        since,
        until,
        result.scanned,
        result.created,
        result.updated,
        result.closed,
        result.skipped_by_acl,
    )
    return result


async def _auto_close(
    db: AsyncSession, *, until: datetime, threshold: float
) -> int:
    """`tasked → closed`：该问题现在能答上来了。

    🔴 **「已命中」必须包含 `faq_hit=True`**。「补料后这条问题已经被沉淀成 FAQ」
       是最常见、也最强有力的闭环证据 —— FAQ 的标准答案就是从这批提问里产出的。
       只认 `allowed_ids`（走检索那条路）会漏掉它。
       而 `faq_hit` 恰好是 `is_miss` 第一条反例**排除**的那一类：**两处口径必须成对地想**，
       不能一处排除、另一处又指望它。
       （be-05 走 FAQ 短路时 `question_embedding` 照落 —— `qa.py:175` 在分支之前赋值。）

    ⚠️ 关闭是**不可逆**的人工可见状态（be-09 spec §10-145）：关错了比不关更糟，
       所以受 `GAP_AUTO_CLOSE_ENABLED` 控制，且不设「连续 N 次命中」之外的额外门槛。
    """
    stmt = select(KnowledgeGap).where(KnowledgeGap.status == STATUS_TASKED)
    taskeds = list((await db.scalars(stmt)).all())
    if not taskeds:
        return 0

    gaps, versions = _with_versions(taskeds)
    if not gaps:
        return 0

    # 🔴 **扫描下界取「这批缺口里最早的水位线」，不是聚合窗口的 `since`。**
    #    用 `since` 会漏掉一类收口：缺口被转建的时刻**晚于**命中提问发生的时刻
    #    （管理员隔了半天才来处理），那条命中提问落在更早的窗口里，
    #    而当时缺口还是 `open`、自动关闭根本没看它 —— 之后每一轮又都扫不到它，
    #    于是这条缺口**永远关不上**，且没有任何报错。
    #    按缺口自己的 `last_seen_at` 起扫，语义正好是「这条缺口最后一次被观察到缺失之后，
    #    这个问题有没有被答上来」。
    floor = min(g.last_seen_at for g in gaps)
    # 取**最近**的行而不是最早的行：收口要的是「现在能不能答上来了」，
    # 截断时丢掉最老的历史比丢掉最新的证据代价小得多。
    hit_stmt = (
        select(QaLog)
        .where(
            QaLog.question_embedding.is_not(None),
            QaLog.created_at > floor,
            QaLog.created_at < until,
        )
        .order_by(QaLog.created_at.desc())
        .limit(_MAX_SCAN)
    )
    hit_rows = list((await db.scalars(hit_stmt)).all())

    closed = 0
    # 按缺口逐个扫命中行。缺口数量是**运营级的**（几十条），不是行数级的，
    # 所以这里不做矩阵优化 —— 用最直白的写法换「一眼能看懂在哪比对」。
    for position, gap in enumerate(gaps):
        vector = versions[position]
        for row in hit_rows:
            if row.created_at <= gap.last_seen_at:
                continue
            answered = row.faq_hit or (
                bool(row.allowed_ids)
                and row.top_score is not None
                and row.top_score >= settings.kb_min_score
            )
            if not answered:
                continue
            candidate = _unit_vector(row.question_embedding)
            if candidate is None:
                continue
            if float(candidate @ vector) < threshold:
                continue
            gap.status = STATUS_CLOSED
            closed += 1
            # 🔴 **状态流转必须留痕**（be-06 §5.4 把这条列在 be-09 名下）。
            #    自动关闭是**系统**动作 ⇒ user_id 记 `system`，不是某个管理员 ——
            #    记成管理员会让人以为「谁手工关的」，而状态历史是运营复盘
            #    「缺口是被补上的还是被关掉的」的唯一依据。
            #    用 `record()`（搭本轮聚合的事务），不是 `record_detached()`：
            #    本函数末尾就 commit，这里再自开一条连接是白多一次写。
            audit_service.record(
                db,
                _SYSTEM_CTX,
                audit_service.AuditAction.GAP_STATUS_UPDATE,
                target_type=audit_service.AuditTargetType.GAP,
                target_id=gap.id,
                detail={
                    "gap_id": gap.id,
                    "from_status": STATUS_TASKED,
                    "to_status": STATUS_CLOSED,
                },
            )
            logger.info(
                "缺口 %s 自动关闭：问题已能被作答（触发 qa_log=%s，faq_hit=%s）",
                gap.id,
                row.id,
                row.faq_hit,
            )
            break
    return closed


def _with_versions(gaps: list[KnowledgeGap]) -> tuple[list[KnowledgeGap], list[np.ndarray]]:
    """拆出「向量可解析」的缺口与它们的**归一化**向量（脏行剔除）。

    `similarity.build_matrix` 做的正是这件事（顺带打脏行告警），
    但它的返回是 `(矩阵, kept 下标)`，而调用方这里是逐条比对 ——
    用 `kept` 把两边对齐，避免「矩阵第 i 行 ≠ 列表第 i 个」这个经典错位。
    """
    matrix, kept = similarity.build_matrix(g.question_embedding for g in gaps)
    return [gaps[i] for i in kept], [matrix[row] for row in range(len(kept))]


# ═══════════════════════════════════════════════════════════════
# 查询
# ═══════════════════════════════════════════════════════════════


async def list_gaps(
    db: AsyncSession, *, min_frequency: int, page: int, page_size: int
) -> tuple[list[KnowledgeGap], int]:
    """缺口清单（按频次倒序，同频次按最近出现时间倒序）。

    ⚠️ **不按调用者部门过滤** —— 这是有意的口径（be-09 spec §5.8）：
    能挂 `gap:list` 的是跨部门的治理角色，按部门过滤会让他们的清单只剩自己那一格。
    代价（`kb_admin` 能读全公司各部的提问原文）与 `faq:candidate:list` 是同一取舍。
    """
    from sqlalchemy import func as sa_func

    count_stmt = select(sa_func.count()).select_from(KnowledgeGap).where(
        KnowledgeGap.frequency >= min_frequency
    )
    total = int((await db.scalar(count_stmt)) or 0)

    stmt = (
        select(KnowledgeGap)
        .where(KnowledgeGap.frequency >= min_frequency)
        .order_by(KnowledgeGap.frequency.desc(), KnowledgeGap.last_seen_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list((await db.scalars(stmt)).all()), total


# ═══════════════════════════════════════════════════════════════
# 一键转建
# ═══════════════════════════════════════════════════════════════


async def _find_open_task(db: AsyncSession, gap_id: str) -> KbImportTask | None:
    """这条缺口是否已经有「还在跑」的补充任务。

    🔴 **这是转建的幂等键，不是查重优化。** `create_supplement_task` 自己 `commit`
       （见其 docstring），所以「建任务」与「置 `tasked`」**不在同一事务**：
       置 `tasked` 失败时会留下一条任务、而缺口仍是 `open`，
       管理员再点一次就会建出第二条 —— 一条缺口补两次料。
       `gap_id` 落库之后才可能认出「这个缺口已经有任务了」，
       于是重试收敛为「复用已有任务 + 补上 `tasked`」，而不是新建。
    """
    stmt = select(KbImportTask).where(
        KbImportTask.gap_id == gap_id,
        KbImportTask.status.in_(KbImportTask.ACTIVE_STATUSES),
    )
    return await db.scalar(stmt)


async def convert_gap_to_task(
    db: AsyncSession, *, gap_id: str, category: str | None, operator: AuthContext
) -> GapTaskResult:
    """一键转建（本模块的**编排器**）：调 be-04 建任务 + 缺口置 `tasked`。

    ⚠️ **名字不是 `create_supplement_task`**：那是 be-04 的**被调用方**，
       签名不同（`(db, gap_id, category, operator)`）。同名却不同形状会让读者
       以为是一份契约的两种写法，实际是「调用者 ↔ 被调用方」两份东西。
       be-09 模块设计 §3.2 已记录这次改名。
    """
    gap = await db.get(KnowledgeGap, gap_id)
    if gap is None:
        raise NotFoundError(f"知识缺口不存在：{gap_id}")
    if gap.status != STATUS_OPEN:
        # ⚠️ 必须是 `ConflictError`(3002/409)，不能是裸 `BizError`
        #    —— 裸 BizError 的 code 默认是 5000，前端会当成服务端故障。
        raise ConflictError(f"该缺口当前状态为 {gap.status}，只有 open 状态可转建")

    task = await _find_open_task(db, gap_id)
    if task is None:
        # be-04 的门面。本模块**不直接** INSERT `kb_import_task`。
        ref = await create_supplement_task(db, gap_id, category, operator)
        task_id = ref.task_id
    else:
        task_id = task.id
        logger.info("缺口 %s 已有在跑的补充任务 %s，复用之（不新建）", gap_id, task_id)

    gap.status = STATUS_TASKED
    if category:
        # 人工传的 category 覆盖模型建议（be-09 模块设计 §4.3）。
        gap.suggested_category = category

    # 留痕搭调用方事务（be-06 `record()` 不 commit）。
    # 动作与键集已在 `audit_service` 冻结：GAP_TASK_CREATE = {gap_id, frequency,
    # suggested_category, task_id}。
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.GAP_TASK_CREATE,
        target_type=audit_service.AuditTargetType.GAP,
        target_id=gap_id,
        detail={
            "gap_id": gap_id,
            "frequency": gap.frequency,
            "suggested_category": gap.suggested_category,
            "task_id": task_id,
        },
    )
    await db.commit()
    return GapTaskResult(gap_id=gap_id, status=STATUS_TASKED, task_id=task_id)
