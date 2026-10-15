"""演示数据灌入 —— 让平台的每一个页面都有真实内容可看。

跑法（项目根目录）：

    uv run python -m backend.scripts.seed_demo            # 灌入
    uv run python -m backend.scripts.seed_demo --clean    # 只清理（回到灌入前）

## 为什么需要它

`seed_org.py` 只灌**组织与权限码**（部门/角色/用户/权限），
`kb_document_permission` 之后一直是空的 —— 而 be-04 的设计是
「新导入文档在管理员显式授权前谁都不能检索」。于是：

- 台账的「权限范围」列**全部**显示「未配置」；
- **任何**用户都检索不到任何内容（A4/A5 无从演示）；
- 看板/治理/缺口页因为 `qa_log` 为空而**一片空白**。

本脚本把这几处填上，让「平台是活的」这件事可以被肉眼验证。

## 🔴 三条纪律

1. **只动 `rag_kb`。** 起手先断言 `settings.mysql_db == "rag_kb"`，
   不对就直接退出 —— 这台 ECS 上还跑着别的库（见项目备忘）。
2. **绝不 wipe 种子数据。** 组织六表**只读不写**，用户/部门/角色全部查现有值，
   不新建（所以本脚本可以在已经用起来的库上反复跑）。
3. **能调真函数的就调真函数**，不手抄一份逻辑：
   ACL 折算走 `lifecycle.derive_acl`，Milvus 同步走
   `milvus_client.refresh_document_acl`，运营聚合走 `stats_service.backfill`，
   缺口聚合走 `gap.aggregate_gaps`。
   手抄一份的下场是「演示数据看着对、真链路上是另一回事」，
   而那**不会报任何错**。

## ⚠️ 本脚本做不到什么（不要把它当成「全链路已通」）

本机**没有 BGE**（`embedding_base_url` 指向 `127.0.0.1:9997`，无人监听）、
**没有 DeepSeek key**、**没有 MinerU token**。所以：

- **问答链路跑不起来** —— 提问要先把问题向量化（BGE）再生成回答（DeepSeek），
  两者都不可达。本脚本灌的是**历史 `qa_log`**，不是「刚问出来的」。
- **FAQ **不是**真聚类出来的** —— `faq_mining` 要算问题向量，
  这里直接插行。治理页能看、能审核，但「挖掘」这一步没有被验证。
- `qa_log.question_embedding` 用的是**替身向量**，见 `_STANDIN_VECTORS` 的说明。
- 导入中心那 16 条 `pending` 的任务需要 Celery worker + BGE 才会动，本脚本不碰它们。

**真跑问答需要**：一台可达的 BGE 服务 + 一个 `DEEPSEEK_API_KEY`。
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import sys
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from backend.clients import milvus_client
from backend.core.db import engine, fresh_session
from backend.core.ids import new_id
from backend.core.settings import settings
from backend.engines.data_mining_engine import gap as gap_engine
from backend.engines.import_engine import lifecycle
from backend.models.faq import Faq, FaqCandidate
from backend.models.kb_document import KbDocument, KbDocumentPermission
from backend.models.kb_chunk import KbChunk
from backend.models.knowledge_gap import KnowledgeGap
from backend.models.qa_log import QaLog, pack_embedding
from backend.models.qa_message import QaMessage
from backend.models.qa_session import QaSession
from backend.models.sys_department import SysDepartment
from backend.models.sys_role import SysRole
from backend.models.sys_user import SysUser
from backend.service import stats_service

# Windows 控制台是 GBK —— 不重配的话 `print` 遇到中文/符号会抛 UnicodeEncodeError，
# 脚本**跑到一半崩掉**且退出前不会 dispose engine，再叠一句
# 「RuntimeError: Event loop is closed」把真信息盖住（CLAUDE.md 已知坑）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

#: 所有自造行的 id 前缀。清理只认它 —— 不认「记住 id」，
#: 因为脚本中途抛异常时只有前缀能保证残留被下一次运行扫掉。
DEMO = "demo"

#: 演示窗口：最近 7 天。`stats_default_range_days` 的默认值就是 7，
#: 比它短会让「近 7 天」的看板图有一截是空的。
WINDOW_DAYS = 7

# 固定种子：跑两次得到**同一批**数据。不加种子的话 `--clean` 之后重灌
# 会得到一份不同的分布，人会对「怎么数字又变了」产生怀疑。
_RNG = random.Random(20261015)


def _log(msg: str) -> None:
    print(f"  {msg}")


def _mark(title: str) -> None:
    print(f"\n== {title} ==")


# ═══════════════════════════════════════════════════════════════
# 演示用的「语料」—— 都是人为编的，但形状贴近真实内部问答
# ═══════════════════════════════════════════════════════════════

#: (问题, 是否有答案)。有答案的会给 `allowed_ids` + 足够高的 `top_score`。
#: 这些问题是照着库里已有的文档分类（制度 / 项目库 / caiwu / 产品手册）编的，
#: 这样「TOP 知识」榜能落到真实存在的文档上。
QUESTIONS: list[tuple[str, bool]] = [
    ("差旅费报销标准是多少？", True),
    ("出差住宿费上限怎么规定的？", True),
    ("年假有多少天？", True),
    ("病假需要什么证明材料？", True),
    ("加班调休怎么申请？", True),
    ("试用期多久？", True),
    ("五险一金的缴纳比例是多少？", True),
    ("绩效考核的周期是怎样的？", True),
    ("项目立项需要哪些材料？", True),
    ("项目结项验收的流程是什么？", True),
    ("报销单审批要几级？", True),
    ("发票丢了还能报销吗？", True),
    ("公司有没有补充医疗保险？", True),
    ("竞业限制协议的补偿标准？", True),
    ("入职需要提交哪些资料？", True),
    ("产品手册里怎么配置权限？", True),
    ("密码忘了怎么重置？", True),
    ("供应商付款账期是多久？", True),
    # ---- 下面这些库里确实没有对应文档，用来喂缺口池 ----
    ("明年公司的战略重点是什么？", False),
    ("竞对公司最近的产品动态？", False),
    ("有没有内部转岗的绿色通道？", False),
    ("海外出差的签证怎么办？", False),
    ("股票期权什么时候能行权？", False),
]

#: 权限挡住的那一类提问：**召回非空、准入为空**。
#: 它们**不该**进缺口池（`is_miss` 的第 ② 条分支），
#: 但要计进 `skipped_by_acl` —— 「缺权限」和「缺知识」是两件事，
#: 混在一起会让运营去补一份**已经存在**的材料。
ACL_BLOCKED_QUESTIONS: list[str] = [
    ("高管薪酬结构是怎么定的？", True),
    ("财务部的月度对账单在哪看？", True),
    ("研发部的技术选型规范有吗？", True),
]


def _standin_vector(question: str, pool: list[list[float]]) -> list[float]:
    """给一个提问取一个**替身向量**。

    🔴 **这不是问题文本的 Embedding —— 别再往下想一层。**
       本机没有 BGE，算不出真向量。这里把问题文本哈希到一个**真实存在**的
       BGE 向量上（从 Milvus 里捞出来的切片向量），只为让
       `_load_miss_rows` 的 `question_embedding IS NOT NULL` 前置条件成立、
       从而让**真的** `aggregate_gaps` 跑起来。

    **它恰好够用在哪**：同一句问题哈希到同一个向量 ⇒ 余弦 1.0 ⇒
    被 `_Matcher.best` 判为「同一条缺口」，于是重复提问真的被累加进
    `frequency`。这正是演示要看到的那个行为。

    **它不够用在哪**：**相似但不同**的问法不会被归并 ——
    那是真 Embedding 才有的能力。所以本脚本只验证「原样重复」这一条路径。
       `run()` 末尾断言「不同 (部门, 问题) 数 == 生成的缺口数」，
       就是防止替身向量**碰巧**把两条不同问题判成同一条（那会静默少建缺口）。
    """
    index = int(hashlib.sha256(question.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[index]


def _pick_vectors(pool_size: int, distinct: int, rng: random.Random) -> list[int]:
    """从池里挑 `distinct` 个**互不相似**的向量下标。

    为什么要挑：替身向量若两两余弦 ≥ `faq_cluster_similarity`(0.85)，
    两条不同问题会被判成同一条缺口 —— 缺口**少建**，而页面上看不出来。
    这里在**上游**保证互不相似，`run()` 末尾再用不变式复核一次。
    """
    idx = list(range(pool_size))
    rng.shuffle(idx)
    return idx[:distinct]


# ═══════════════════════════════════════════════════════════════
# 0. 前置
# ═══════════════════════════════════════════════════════════════


def _assert_target_db() -> None:
    """只准动 `rag_kb`。

    这台 ECS 上的 MySQL 曾经被勒索过，用户明确要求「只动 rag_kb，别碰其余库」。
    `mysql_db` 配错（或某天有人改了 `.env`）时，本脚本灌的是**别人的库** ——
    所以这条断言放在最前面，且**失败即退出**，不做「尽力而为」。
    """
    if settings.mysql_db != "rag_kb":
        print(f"❌ 目标库是 {settings.mysql_db!r}，本脚本只允许对 'rag_kb' 操作。已退出。")
        sys.exit(2)


# ═══════════════════════════════════════════════════════════════
# 1. 权限：kb_document_permission
# ═══════════════════════════════════════════════════════════════

#: 授权方案。用 `enabled` 文档 id 的哈希取模来选，**不用随机** ——
#: 随机会让 `--clean` 之后重灌得到另一批文档带权限，
#: 于是「上次能搜到的这次搜不到了」，排查时无从下手。
#: 取模分桶让分配**可复现**，且四类维度都有样本。
def _plan_for(doc_id: str, dept_ids: list[str], role_ids: list[str], user_ids: list[str]):
    """文档 id → 权限行计划。返回 `[(entity_type, entity_id), ...]`。"""
    bucket = int(hashlib.sha256(doc_id.encode()).hexdigest(), 16) % 100
    if bucket < 38:
        return [(KbDocumentPermission.ENTITY_GLOBAL, KbDocumentPermission.GLOBAL_ENTITY_ID)]
    if bucket < 60:
        n = 1 + bucket % 2
        return [
            (KbDocumentPermission.ENTITY_DEPARTMENT, dept_ids[(bucket + i) % len(dept_ids)])
            for i in range(n)
        ]
    if bucket < 78:
        return [(KbDocumentPermission.ENTITY_ROLE, role_ids[bucket % len(role_ids)])]
    if bucket < 92:
        n = 1 + bucket % 3
        return [
            (KbDocumentPermission.ENTITY_USER, user_ids[(bucket + i * 3) % len(user_ids)])
            for i in range(n)
        ]
    # 最后一段给**多维度**样本：前端 `permission_summary` 的「A / B」文案
    # 只有多维度文档才渲染得出来，全是单维度的话那行代码等于没被看过。
    return [
        (KbDocumentPermission.ENTITY_DEPARTMENT, dept_ids[bucket % len(dept_ids)]),
        (KbDocumentPermission.ENTITY_USER, user_ids[(bucket * 7) % len(user_ids)]),
    ]


async def _seed_permissions(db, dept_ids, role_ids, user_ids) -> list[str]:
    _mark("1. 文档权限（kb_document_permission）+ 同步 Milvus acl_*")
    docs = list(
        (await db.scalars(select(KbDocument).where(KbDocument.deleted_at.is_(None)))).all()
    )
    enabled = [d for d in docs if d.status == KbDocument.STATUS_ENABLED]
    _log(f"文档 {len(docs)} 篇，其中 enabled {len(enabled)} 篇")

    # 🔴 **草稿也要配权限**（2026-10-15 修）。首版只给 enabled 的配，
    #    理由是「草稿不参与检索」—— 技术上没错，但**演示效果是反的**：
    #    台账默认按 `created_at DESC` 排，而那 16 篇草稿正好是最新导入的一批，
    #    于是**第一页 5 条一条权限标签都没有**，看起来像「权限功能没实现」。
    #    而且「先配好权限再启用」本来就是管理员会走的路子，给草稿配权限并不越界。
    #    Milvus 的 ACL 只对**有切片**的文档刷（草稿没有切片，刷也是空转）。
    doc_ids = [d.id for d in docs]
    # 先清掉这些文档的旧权限行再灌 —— 让本步骤**幂等**。
    # 只清本脚本负责的这批文档，不动别人（`kb_document_permission` 里
    # 原本就是空的，所以这里实际上是从零开始）。
    await db.execute(
        delete(KbDocumentPermission).where(KbDocumentPermission.document_id.in_(doc_ids))
    )

    rows: list[KbDocumentPermission] = []
    for doc_id in doc_ids:
        for entity_type, entity_id in _plan_for(doc_id, dept_ids, role_ids, user_ids):
            rows.append(
                KbDocumentPermission(
                    id=new_id("docperm"),
                    document_id=doc_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                )
            )
    db.add_all(rows)
    await db.commit()

    counts: dict[str, int] = {}
    for row in rows:
        counts[row.entity_type] = counts.get(row.entity_type, 0) + 1
    _log(f"写入权限行 {len(rows)} 条：{counts}")

    # ---- 🔴 配对写：Milvus 侧 ----
    # `kb_document_permission` 是**权威**，Milvus 的 `acl_*` 是**冗余副本**
    # （be-05 的前置过滤直接读 Milvus）。只写 MySQL 不刷 Milvus 的后果是：
    # 台账上明明写着「全局」，而检索**一条都召回不到** ——
    # 前置过滤按旧的空 ACL 把候选全挡了，**且不报错**。
    # 权限接口（be-04 的 `update_permissions`）里有这一步，灌数据时同样不能漏。
    _log("同步 Milvus acl_*（缺这一步会让检索恒空，而台账看着完全正常）…")
    by_doc: dict[str, list[KbDocumentPermission]] = {}
    for row in rows:
        by_doc.setdefault(row.document_id, []).append(row)

    # 只刷**有切片**的文档：草稿没有切片，`refresh_document_acl` 会白跑一趟
    # （它自己会返回 0，但一次 Milvus 往返 0.8s × 16 篇是纯浪费）。
    with_chunks = {d.id: d.chunk_count for d in docs if (d.chunk_count or 0) > 0}
    _log(f"其中有切片的 {len(with_chunks)} 篇需要刷 Milvus ACL")

    synced = 0
    failed = 0
    for doc_id in doc_ids:
        if doc_id not in with_chunks:
            continue
        acl = lifecycle.derive_acl(by_doc.get(doc_id, []))  # 唯一计算入口，不手抄
        try:
            synced += await milvus_client.refresh_document_acl(doc_id, acl)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _log(f"⚠️ 文档 {doc_id} 的 Milvus ACL 同步失败：{type(exc).__name__}: {exc}")
    _log(f"Milvus 切片 ACL 已刷新 {synced} 条（失败 {failed} 篇）")

    # 🔴 失败不静默：刷不动 Milvus 就意味着**检索恒空**，而台账上权限标签
    #    全都正常显示 —— 两端不一致却没有任何一处报错。
    #    本脚本宁可在这里吵，也不让「看着配好了、其实搜不到」上演示台。
    if failed:
        raise RuntimeError(
            f"{failed} 篇文档的 Milvus ACL 未刷新 —— 检索会恒空，"
            "先修 Milvus 连接再灌数据（这不是能忽略的警告）"
        )
    return doc_ids


async def _clear_milvus_acl(doc_ids: list[str]) -> None:
    """`--clean` 时把 Milvus 的 ACL 复位成「谁都不许」。

    不复位的后果：MySQL 的权限行删了、Milvus 还留着 `acl_global=true`，
    于是**权限被撤销的文档仍然检索得到** —— 这正是 be-05 后置兜底要拦的场景，
    但我们不该在演示库里制造它。
    """
    empty = {"acl_global": False, "acl_departments": [], "acl_roles": [], "acl_users": []}
    total = 0
    for doc_id in doc_ids:
        try:
            total += await milvus_client.refresh_document_acl(doc_id, empty)
        except Exception:  # noqa: BLE001
            pass
    _log(f"Milvus 切片 ACL 已复位 {total} 条")


# ═══════════════════════════════════════════════════════════════
# 2. 问答日志：qa_session / qa_message / qa_log
# ═══════════════════════════════════════════════════════════════


def _spread(days: int, count: int, rng: random.Random) -> list[datetime]:
    """在最近 `days` 天里撒 `count` 个时刻，带上**工作时间的昼夜节律**。

    均匀撒的结果是直方图与趋势图一条平线，看不出「图有没有在工作」；
    真实内网问答集中在 9–19 点，做个钟形权重才像真的。
    """
    now = datetime.now()
    out: list[datetime] = []
    for _ in range(count):
        day = now - timedelta(days=rng.randrange(days), hours=0)
        # 9–19 点双峰（上午一波、下午一波）
        hour = rng.choice([9, 10, 10, 11, 11, 14, 14, 15, 15, 16, 16, 17, 17, 18])
        moment = day.replace(hour=hour, minute=rng.randrange(60), second=rng.randrange(60),
                             microsecond=0)
        if moment >= now:
            moment = now - timedelta(minutes=rng.randrange(5, 120))
        out.append(moment)
    return sorted(out)


async def _seed_qa(db, users, chunk_pool: list[str], vector_pool: list[list[float]]):
    """灌 `qa_session` / `qa_message` / `qa_log`。"""
    _mark("2. 问答会话与问答日志（qa_session / qa_message / qa_log）")

    now = datetime.now()
    start = (now - timedelta(days=WINDOW_DAYS)).replace(minute=0, second=0, microsecond=0)

    # ---- 会话：12 条，分给几个用户 ----
    sessions: list[QaSession] = []
    speakers = users[:6]
    for i in range(12):
        user = speakers[i % len(speakers)]
        created = start + timedelta(hours=6 + i * 13, minutes=_RNG.randrange(60))
        if created >= now:
            created = now - timedelta(hours=1)
        sessions.append(
            QaSession(
                id=new_id("demosess"),
                user_id=user.id,
                title=QUESTIONS[i % len(QUESTIONS)][0][:30],
                message_count=0,
                created_at=created,
                updated_at=created,
            )
        )
    db.add_all(sessions)
    await db.flush()
    _log(f"会话 {len(sessions)} 条")

    # ---- 每条会话配一问一答，让「历史会话」页点进去有内容 ----
    messages: list[QaMessage] = []
    for i, sess in enumerate(sessions):
        question, answerable = QUESTIONS[i % len(QUESTIONS)]
        messages.append(
            QaMessage(
                id=new_id("demomsg"),
                session_id=sess.id,
                role=QaMessage.ROLE_USER,
                content=question,
                citations=None,
                blocked_count=0,
                created_at=sess.created_at,
            )
        )
        messages.append(
            QaMessage(
                id=new_id("demomsg"),
                session_id=sess.id,
                role=QaMessage.ROLE_ASSISTANT,
                content=(
                    f"根据《{_doc_label(i)}》的规定：{question.rstrip('？')}的说明如下……"
                    if answerable
                    else "知识库中暂时没有找到与这个问题相关的内容。"
                ),
                citations=[] if not answerable else None,
                blocked_count=0,
                created_at=sess.created_at + timedelta(seconds=_RNG.randrange(3, 12)),
            )
        )
        sess.message_count = 2
    db.add_all(messages)
    _log(f"消息 {len(messages)} 条")

    # ---- 日志：600 条。这是看板与缺口池的**唯一**输入 ----
    total = 600
    moments = _spread(WINDOW_DAYS, total, _RNG)
    # 目标混合：FAQ 命中 12%、真命中 48%、缺权限 10%、真缺口 30%。
    # 缺口占比刻意调高：A8 的缺口页要有东西可看，
    # 而真实环境缺口是少数（这个比例**不代表生产实际**，只是把每条路径都点亮）。
    logs: list[QaLog] = []
    for i, moment in enumerate(moments):
        roll = _RNG.random()
        user = users[_RNG.randrange(len(users))]
        session = sessions[_RNG.randrange(len(sessions))]

        if roll < 0.12:
            question = _RNG.choice(QUESTIONS)[0]
            faq_hit, allowed, recalled, top_score = True, [], [], None
            token_in, token_out = 0, 0  # FAQ 直接作答，不调 LLM
        elif roll < 0.60:
            question, _ok = _RNG.choice([q for q in QUESTIONS if q[1]])
            n = _RNG.randrange(1, 4)
            allowed = [_RNG.choice(chunk_pool) for _ in range(n)]
            faq_hit, recalled = False, allowed
            top_score = round(_RNG.uniform(0.55, 0.92), 4)
            token_in, token_out = _RNG.randrange(900, 2600), _RNG.randrange(120, 600)
        elif roll < 0.70:
            # 召回得到、但全被权限挡住 ⇒ **缺权限不是缺知识**
            question = _RNG.choice(ACL_BLOCKED_QUESTIONS)[0]
            allowed = []
            recalled = [_RNG.choice(chunk_pool) for _ in range(_RNG.randrange(1, 4))]
            faq_hit = False
            top_score = round(_RNG.uniform(0.5, 0.88), 4)
            token_in, token_out = _RNG.randrange(900, 2200), _RNG.randrange(60, 200)
        else:
            question = _RNG.choice([q for q in QUESTIONS if not q[1]])[0]
            faq_hit, allowed, recalled, top_score = False, [], [], None
            token_in, token_out = _RNG.randrange(700, 1800), _RNG.randrange(40, 160)

        logs.append(
            QaLog(
                id=new_id("demoqalog"),
                session_id=session.id,
                user_id=user.id,
                department_id=user.department_id,
                question=question,
                # 🔴 替身向量，见 `_standin_vector` 的长注释。
                #    置 NULL 会让 `aggregate_gaps` 直接扫不到这一行
                #    （`_load_miss_rows` 有 `question_embedding IS NOT NULL`）。
                question_embedding=(
                    None if allowed or faq_hit else _pack_vector(_standin_vector(question, vector_pool))
                ),
                recalled_ids=recalled,
                allowed_ids=allowed,
                blocked_ids=[],
                top_score=top_score,
                token_in=token_in,
                token_out=token_out,
                latency_ms=(
                    _RNG.randrange(40, 120)
                    if faq_hit
                    else min(_RNG.randrange(600, 6800), 9000)
                ),
                faq_hit=faq_hit,
                created_at=moment,
            )
        )
    db.add_all(logs)
    await db.commit()
    _log(f"问答日志 {len(logs)} 条，时间跨度 [{(now - timedelta(days=WINDOW_DAYS)):%m-%d} , {now:%m-%d}]")
    return start, now


def _pack_vector(vector: list[float]) -> bytes:
    """`list[float]` → `BLOB`。**必须与 `similarity` 的读法同口径**（float32 小端）。

    自己 `struct.pack` 一遍是刻意的：读侧 `similarity.build_matrix` 认的是
    `float32`，写成 `float64` 的话它按 3064 字节解析 3072 字节的 blob，
    结果是**解析出的向量整段错位** —— 余弦值全变，匹配全失效，且不报错。
    `verify_gap.py` 的 T1 里有守这条的用例。
    """
    import numpy as np

    return np.asarray(vector, dtype=np.float32).tobytes()


def _doc_label(index: int) -> str:
    """编一个像样的文档名，只用于问答消息的正文。"""
    names = ["差旅费管理办法", "考勤与休假制度", "项目管理制度", "薪酬福利手册", "采购付款规范"]
    return names[index % len(names)]


# ═══════════════════════════════════════════════════════════════
# 3. 运营看板聚合
# ═══════════════════════════════════════════════════════════════


async def _seed_stats(db, start: datetime, end: datetime) -> None:
    _mark("3. 运营看板聚合（调 stats_service.backfill —— 真函数）")
    # **不手写 stats_hourly / stats_daily 的行**。手工算一遍指标，
    # 得到的是「我以为是怎样」而不是「代码算出来是怎样」；
    # 而且一旦聚合逻辑改了，假的种子数据不会跟着变 —— 看板会显示
    # 一份**只存在于种子脚本里**的口径。这里调的就是 beat 每 10 分钟调的那个。
    summary = await stats_service.backfill(db, start, end)
    _log(f"小时桶 {summary.get('hour', 0)} 行 / 日桶 {summary.get('day', 0)} 行")


# ═══════════════════════════════════════════════════════════════
# 4. FAQ：faq / faq_candidate
# ═══════════════════════════════════════════════════════════════

FAQ_SEED: list[tuple[str, str, bool, str]] = [
    ("差旅费报销标准是多少？",
     "按《差旅费管理办法》：市内交通实报实销；住宿费一线城市每晚上限 600 元、"
     "其他城市 450 元；伙食补助每人每天 100 元。超过上限部分需部门负责人书面说明。",
     True, "approved"),
    ("出差住宿费上限怎么规定的？",
     "一线城市（北上广深）每晚上限 600 元，省会城市 500 元，其他城市 450 元。"
     "同性别同事同行原则上合住标准间。",
     True, "approved"),
    ("年假有多少天？",
     "累计工作满 1 年不满 10 年：5 天；满 10 年不满 20 年：10 天；满 20 年：15 天。"
     "入职当年按剩余日历天数折算。",
     True, "approved"),
    ("报销单审批要几级？",
     "单笔 5000 元以下：直属主管 + 部门负责人；5000 元及以上：再加财务负责人；"
     "2 万元及以上需总经理审批。",
     True, "approved"),
    ("发票丢了还能报销吗？",
     "可以。需提供开票方加盖发票专用章的存根复印件或电子发票重复下载件，"
     "并附情况说明，由部门负责人签字确认。",
     True, "approved"),
    ("五险一金的缴纳比例是多少？",
     "以北京市为例：养老单位 16%、个人 8%；医疗单位 9.8%、个人 2%+3 元；"
     "公积金单位与个人各 12%。基数按上年度月平均工资核定。",
     True, "pending"),
    ("公司有没有补充医疗保险？",
     "有。入职转正后自动参保，覆盖社保目录内个人自付部分，年度限额 2 万元，"
     "理赔通过保险服务商 App 提交。",
     True, "pending"),
    ("试用期多久？",
     "合同期 3 年以上（含）为 6 个月；1 年以上不满 3 年为 2 个月；"
     "3 个月以上不满 1 年为 1 个月。",
     True, "approved"),
    ("密码忘了怎么重置？",
     "本平台暂未开放自助重置，请联系系统管理员在「组织管理 → 用户」里重置密码。",
     True, "pending"),
    ("加班调休怎么申请？",
     "加班前需在 OA 提交加班申请并获主管批准；法定节假日加班按 3 倍工资，"
     "休息日加班优先安排调休，调休需在 3 个月内使用。",
     True, "rejected"),
]

CANDIDATES_SEED: list[tuple[str, str, int, float, str]] = [
    ("项目立项需要哪些材料？",
     "立项申请书、可行性研究报告、预算明细表、部门负责人意见，以及涉及技术选型时的评审纪要。",
     23, 0.93, "pending"),
    ("项目结项验收的流程是什么？",
     "项目经理提交结项报告 → 技术负责人评审 → 财务核对预算执行 → 项目管理办公室归档。",
     18, 0.90, "pending"),
    ("病假需要什么证明材料？", "连续 3 天以上病假需提供二级及以上医院开具的病假证明与就诊记录。",
     15, 0.88, "pending"),
    ("入职需要提交哪些资料？",
     "身份证、学历学位证、离职证明、体检报告、银行卡复印件，以及一寸照片两张。",
     11, 0.86, "pending"),
    ("供应商付款账期是多久？", "标准账期为验收合格并收到发票后 30 天；战略供应商可谈至 60 天。",
     9, 0.87, "approved"),
    ("绩效考核的周期是怎样的？", "季度考核 + 年度综合评定。季度考核结果影响季度奖金，年度结果影响调薪与晋升。",
     8, 0.85, "approved"),
    ("产品手册里怎么配置权限？", "在「知识管理 → 文档台账」中打开文档的权限弹窗，按全局/部门/角色/用户四个维度授权。",
     7, 0.91, "approved"),
    ("竞对公司最近的产品动态？", "（该问题无可用答案，作为缺口样本保留）", 4, 0.31, "rejected"),
    ("明年公司的战略重点是什么？", "（该问题无可用答案，作为缺口样本保留）", 3, 0.28, "rejected"),
    ("有没有内部转岗的绿色通道？", "（该问题无可用答案，作为缺口样本保留）", 2, 0.25, "rejected"),
]


async def _seed_faq(db, admin_id: str) -> None:
    _mark("4. FAQ（faq / faq_candidate）—— 直接插行，**不是**真聚类出来的")
    _log("⚠️ 候选是直接插的行：`faq_mining` 的聚类**没有被这一步验证**。")
    _log("   但**已发布 FAQ 的向量是真算的**（见下）—— 否则缓存索引装载 0 条。")

    candidates: list[FaqCandidate] = []
    for question, answer, freq, conf, status in CANDIDATES_SEED:
        reviewed = status in ("approved", "rejected")
        created = datetime.now() - timedelta(days=_RNG.randrange(1, 6))
        candidates.append(
            FaqCandidate(
                id=new_id("democand"),
                # `cluster_key` 带 demo 前缀：清理时靠它兜底，
                # 也让人一眼看出这条不是挖掘出来的。
                cluster_key=f"{DEMO}_cluster_{hashlib.sha256(question.encode()).hexdigest()[:16]}",
                question=question,
                answer=answer,
                frequency=freq,
                confidence=conf,
                related_doc_ids=None,
                status=status,
                reviewer_id=admin_id if reviewed else None,
                reviewed_at=created + timedelta(hours=6) if reviewed else None,
                created_at=created,
            )
        )
    db.add_all(candidates)
    await db.flush()

    faqs: list[Faq] = []
    approved = [c for c in candidates if c.status == "approved"]
    for question, answer, cache_enabled, status in FAQ_SEED:
        source = next((c for c in approved if c.question == question), None)
        if status != "approved":
            # 非 approved 的条目**不发布**，只留候选 ——
            # 「已发布 FAQ」列表里混进待审内容是很难发现的数据不一致。
            continue
        faqs.append(
            Faq(
                id=new_id("demofaq"),
                question=question,
                answer=answer,
                question_embedding=None,  # 下面统一现算
                cache_enabled=cache_enabled,
                source_candidate_id=source.id if source else None,
                published_by=admin_id,
                published_at=datetime.now() - timedelta(days=_RNG.randrange(1, 5)),
                hit_count=_RNG.randrange(0, 40),
            )
        )

    filled, missing = await _refill_faq_vectors(faqs)

    db.add_all(faqs)
    await db.commit()
    _log(f"候选 {len(candidates)} 条（pending/approved/rejected 混合）、已发布 FAQ {len(faqs)} 条")
    _log(f"其中启用缓存的 {sum(1 for f in faqs if f.cache_enabled)} 条")
    if missing:
        _log(f"⚠️ {missing} 条 FAQ 没算出向量（BGE 不可达）—— 缓存装载会跳过它们，")
        _log("   表现为「启用缓存也不加速」。起好 BGE 后跑 --refill-faq-vectors 补上。")


async def _refill_faq_vectors(faqs: list[Faq]) -> tuple[int, int]:
    """给已发布 FAQ 现算 `question_embedding`，返回 (成功数, 失败数)。

    🔴 **必须走 `retrieval.embed_query`（带 BGE 查询前缀），不能用裸 `embed_texts`。**
       这条向量唯一的用途就是被**用户提问**去比（`faq_cache.search` 拿
       `query_dense` 比它），而 `query_dense` 出自 `embed_query`，是带前缀的。
       两侧口径不一致时，实测（本机 BGE）同一句问题余弦只有 0.83 左右，
       而 `faq_match_threshold = 0.92` ⇒ **一字不差地问也命中不了**。
       详见 `engines/data_mining_engine/faq_mining.py` 发布路径的长注释，
       守门用例 `verify_faq.py::t1_publish_embedding_side`。

    ⚠️ 算不出来**不阻断**灌数：留 NULL，缓存少一条，走正常检索路径 ——
       功能降级不是故障（与 `faq_mining` 发布路径同一取舍）。
    """
    if not faqs:
        return 0, 0

    from backend.engines.search_engine import retrieval

    ok = fail = 0
    for faq in faqs:
        try:
            vector = await retrieval.embed_query(faq.question)
        except Exception as exc:  # noqa: BLE001 —— 见 docstring：降级不阻断
            _log(f"  算向量失败（{type(exc).__name__}）：{faq.question[:20]}…")
            fail += 1
            continue
        if vector:
            faq.question_embedding = pack_embedding(vector)
            ok += 1
        else:
            fail += 1
    _log(f"FAQ 向量：算出 {ok} 条，未算出 {fail} 条")
    return ok, fail


# ═══════════════════════════════════════════════════════════════
# 5. 知识缺口
# ═══════════════════════════════════════════════════════════════


async def _seed_gaps(db, start: datetime, end: datetime) -> bool:
    _mark("5. 知识缺口（调 gap.aggregate_gaps —— 真函数）")

    before = int(await db.scalar(select(func.count()).select_from(KnowledgeGap)) or 0)
    # 清掉上一轮的 demo 缺口，否则水位线会让重复提问「已经计过」而不再累加，
    # 跑第二遍时 frequency 全是旧值 —— 看起来像聚合坏了。
    await db.execute(
        delete(KnowledgeGap).where(KnowledgeGap.id.like(f"{DEMO}%"))
    )
    await db.commit()

    result = await gap_engine.aggregate_gaps(db, since=start, until=end)
    await db.commit()
    data = result.as_dict()
    _log(f"扫描 {data.get('scanned')} 条未命中候选：新建 {data.get('created')}、"
         f"累加 {data.get('updated')}、自动关闭 {data.get('closed')}、"
         f"权限挡住跳过 {data.get('skipped_by_acl')}")
    _log("⚠️ `suggested_category` 会是空的 —— 那是 DeepSeek 的可选装饰，本机没配 key。")

    after = int(await db.scalar(select(func.count()).select_from(KnowledgeGap)) or 0)
    _log(f"缺口表 {before} → {after} 行")

    # ---- 不变式：替身向量没有把不同问题误判成同一条 ----
    # 这是**唯一**能发现「替身向量碰巧相似 ⇒ 缺口少建」的地方。
    # 少了它，缺口页显示 8 条而不是 12 条时，没人知道是数据少还是逻辑错。
    rows = list(
        (await db.scalars(select(KnowledgeGap).order_by(KnowledgeGap.id))).all()
    )
    distinct = {(g.question, g.department_id) for g in rows}
    ok = len(distinct) == len(rows)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 缺口条数 == 不同(问题,部门)组合数"
          f"（{len(rows)} vs {len(distinct)}）—— 替身向量未误并")
    return ok


# ═══════════════════════════════════════════════════════════════
# 6. 自检：把每个页面真正会调的接口走一遍
# ═══════════════════════════════════════════════════════════════


async def _verify_pages() -> bool:
    """用真 HTTP（ASGI）把 5 个看板接口 + 治理/缺口/台账走一遍，打印关键数字。

    🔴 **只断言「接口回 `code == 0`」是不够的** —— 一个恒空的看板同样回 200。
       这里逐条打印**非零**的具体数值，人一眼就能看出有没有东西。
       （be-07 踩过：路径少一段导致 5 张图全 404，而接口侧"工作正常"。）
    """
    from httpx import ASGITransport, AsyncClient

    import main

    _mark("6. 自检：把页面真正调的接口走一遍")
    ok = True
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://verify") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "Rag@12345"})
        token = r.json()["data"]["access_token"]
        h = {"Authorization": f"Bearer {token}"}

        checks: list[tuple[str, str, object]] = []

        def rows_of(payload: object) -> list:
            """榜类端点回**裸数组**（`list[TopQuestionItem]`），分页端点回 `{list,...}`。

            两种都认 —— 但**不是**「形状没定」的权宜之计：
            `get_top_questions` 的返回注解就是 `list[...]`，是真的裸数组。
            """
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                return payload.get("list") or []
            return []

        r = await c.get("/api/stats/dashboard/overview", headers=h)
        d = r.json().get("data") or {}
        checks.append(("运营总览", "pv", d.get("pv")))
        checks.append(("运营总览", "知识单元总数", d.get("document_count")))

        r = await c.get("/api/stats/dashboard/trend", headers=h)
        d = r.json().get("data") or {}
        checks.append(("趋势图", "点数", len(d.get("points") or [])))

        r = await c.get("/api/stats/dashboard/top-questions", headers=h)
        checks.append(("热门问题", "条数", len(rows_of(r.json().get("data")))))

        r = await c.get("/api/stats/dashboard/top-knowledge", headers=h)
        checks.append(("热门知识", "条数", len(rows_of(r.json().get("data")))))

        r = await c.get("/api/stats/dashboard/latency", headers=h)
        d = r.json().get("data") or {}
        # 直方图是**两条平行数组**（`edges` / `counts`），不是一个 {bucket,count} 列表
        counts = d.get("counts") or []
        checks.append(("延时分布", "样本数", sum(int(x) for x in counts) if counts else 0))
        checks.append(("延时分布", "P95(ms)", d.get("p95_ms")))

        r = await c.get("/api/mining/faq", params={"page": 1, "page_size": 20}, headers=h)
        checks.append(("FAQ 已发布", "total", (r.json().get("data") or {}).get("total")))

        r = await c.get("/api/mining/faq/candidates", params={"page": 1, "page_size": 20}, headers=h)
        checks.append(("FAQ 候选", "total", (r.json().get("data") or {}).get("total")))

        r = await c.get("/api/mining/gaps", params={"page": 1, "page_size": 20}, headers=h)
        checks.append(("知识缺口", "total", (r.json().get("data") or {}).get("total")))

        r = await c.get("/api/import/documents", params={"page": 1, "page_size": 5}, headers=h)
        rows = ((r.json().get("data") or {}).get("list")) or []
        with_perm = sum(1 for x in rows if x.get("permission_scopes"))
        checks.append(("文档台账", "本页带权限的", f"{with_perm}/{len(rows)}"))

    for page, label, value in checks:
        # 0 / 空 就是「这个页面还是白板」—— 那正是本脚本要消灭的状态。
        good = bool(value)
        ok = ok and good
        print(f"  [{'OK  ' if good else 'FAIL'}] {page:<10} {label} = {value}")
    return ok


# ═══════════════════════════════════════════════════════════════
# 清理
# ═══════════════════════════════════════════════════════════════


async def _clean() -> None:
    _mark("清理演示数据（只删带 demo 前缀的行 + 本脚本配过权限的那批文档）")
    async with fresh_session() as db:
        enabled_ids = list(
            (
                await db.scalars(
                    select(KbDocument.id).where(
                        KbDocument.deleted_at.is_(None),
                        KbDocument.status == KbDocument.STATUS_ENABLED,
                    )
                )
            ).all()
        )

        # ⚠️ 这条删除**不认前缀**（`kb_document_permission` 没有可标记的列），
        #    删的是「全部 enabled 文档的权限行」。这是刻意的：
        #    本脚本灌数据前那张表**本来就是空的**（be-04 的现状），
        #    而权限行的 id 是随机生成、不留痕的。
        #    代价：若有人在这之后手工配过权限，`--clean` 会一并删掉。
        #    所以清理是**显式动作**（`--clean`），不做自动回滚。
        await db.execute(
            delete(KbDocumentPermission).where(
                KbDocumentPermission.document_id.in_(enabled_ids)
            )
        )
        for model in (QaLog, QaMessage, QaSession, Faq, FaqCandidate, KnowledgeGap):
            await db.execute(delete(model).where(model.id.like(f"{DEMO}%")))
        # 候选的 `cluster_key` 也带前缀 —— id 前缀万一对不上时还有这条兜底。
        await db.execute(
            delete(FaqCandidate).where(FaqCandidate.cluster_key.like(f"{DEMO}\\_%", escape="\\"))
        )
        await db.commit()
        _log("MySQL 侧的演示行已删；组织六表与真实文档**未动**")

    await _clear_milvus_acl(enabled_ids)
    _log("Milvus ACL 已复位")
    _log("⚠️ stats_hourly / stats_daily 是**聚合表**，本脚本不删它们 ——")
    _log("   它们的值是 qa_log 的重算结果，qa_log 清空后跑一次 beat 的")
    _log("   aggregate_hourly 就会被重算成 0（预聚合是「重算覆盖」不是「累加」）。")


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════


async def _load_context(db):
    """读**已有**的组织数据。只读不写 —— 本脚本绝不新建用户/部门/角色。"""
    dept_ids = list((await db.scalars(select(SysDepartment.id))).all())
    role_ids = list((await db.scalars(select(SysRole.id))).all())
    users = list((await db.scalars(select(SysUser).order_by(SysUser.id))).all())
    admin = await db.scalar(select(SysUser).where(SysUser.username == "admin"))
    if not (dept_ids and role_ids and users and admin):
        print("❌ 组织数据不完整 —— 先跑 `uv run python -m backend.scripts.seed_org`。已退出。")
        await engine.dispose()
        sys.exit(3)

    # 切片池：**真的** chunk id（看板的「TOP 知识」要靠它反查文档标题，
    # 编一个不存在的 id 会让榜首永远查不到标题 —— be-08 踩过同款）。
    chunk_pool = list(
        (
            await db.scalars(
                select(KbChunk.id).where(KbChunk.document_id.in_(
                    select(KbDocument.id).where(
                        KbDocument.deleted_at.is_(None),
                        KbDocument.status == KbDocument.STATUS_ENABLED,
                    )
                )).limit(400)
            )
        ).all()
    )
    _log(f"组织：{len(dept_ids)} 部门 / {len(role_ids)} 角色 / {len(users)} 用户；"
         f"可用切片 {len(chunk_pool)} 条")
    return dept_ids, role_ids, users, admin, chunk_pool


async def _load_vector_pool() -> list[list[float]]:
    """从 Milvus 捞一批**真实**的 BGE 向量当替身池（见 `_standin_vector`）。

    用真向量而不是随机数：随机向量在 768 维里两两几乎正交（余弦≈0），
    虽然也能跑，但那样连「阈值 0.85」这个数字都没被真正碰到过 ——
    用真向量的分布至少让相似度落在真实区间里。
    """
    try:
        client = milvus_client.get_client()
        rows = await milvus_client._call(
            "query",
            lambda: client.query(
                collection_name=settings.milvus_collection,
                filter="", output_fields=["dense"], limit=400,
            ),
        )
        pool = [list(r["dense"]) for r in rows if r.get("dense")]
        _log(f"从 Milvus 取到 {len(pool)} 条真实向量作替身池")
        return pool
    except Exception as exc:  # noqa: BLE001
        _log(f"⚠️ 取向量池失败（{type(exc).__name__}: {exc}），缺口聚合将无可匹配数据")
        return []


async def _guard_already_seeded(db) -> bool:
    """已经灌过就**拒绝再灌**，返回 True 表示应当中止。

    🔴 本脚本对 `faq` / `qa_session` / `qa_message` / `qa_log` **不是幂等的**
       （id 是随机生成的），重复跑会把演示数据成倍放大：
       FAQ 6 → 18、qa_log 600 → 1800。而**症状全是"看起来正常"** ——
       看板的 PV 虚高、TOP 榜每一条都出现三次、FAQ 列表三份重复，
       没有任何报错，只会让人以为"数据就是这么设计的"。

    判据用 `qa_log` 的 demo 行数而不是"表非空"：`kb_document` 等表里有
    真实文档，按表非空判会把正常场景也拦下来。qa_log 的 demo 行只可能来自本脚本。
    """
    existing = (
        await db.execute(
            select(func.count()).select_from(QaLog).where(QaLog.id.like(f"{DEMO}%"))
        )
    ).scalar() or 0
    if existing:
        print(
            f"❌ 检测到已灌过演示数据（{DEMO} 前缀的 qa_log 有 {existing} 行）—— 已中止，"
            "什么都没写。\n"
            "   本脚本对 FAQ / 会话 / 问答日志**不是幂等的**，直接重跑会把它们成倍放大。\n"
            "   要重置请先 `--clean`（它只删 demo 前缀的行，组织表与真实文档不动），"
            " 再重跑本命令。"
        )
        return True
    return False


async def _run() -> int:
    _assert_target_db()
    async with fresh_session() as db:
        if await _guard_already_seeded(db):
            return 4
        dept_ids, role_ids, users, admin, chunk_pool = await _load_context(db)

        vector_pool = await _load_vector_pool()
        if not vector_pool:
            print("❌ 拿不到向量池，缺口这一条链路没法演示。已退出。")
            return 3

        await _seed_permissions(db, dept_ids, role_ids, [u.id for u in users])
        await _seed_qa(db, users, chunk_pool, vector_pool)

    # 上面那个 session 关了；下面几步各自需要独立事务
    window_start = (datetime.now() - timedelta(days=WINDOW_DAYS)).replace(
        minute=0, second=0, microsecond=0
    )
    window_end = datetime.now() + timedelta(minutes=1)

    async with fresh_session() as db:
        await _seed_stats(db, window_start, window_end)
        await _seed_faq(db, admin.id)

    async with fresh_session() as db:
        gaps_ok = await _seed_gaps(db, window_start, window_end)

    pages_ok = await _verify_pages()

    print(f"\n{'=' * 60}")
    print(f"演示数据已就位。缺口不变式：{'OK' if gaps_ok else 'FAIL'}；页面自检：{'OK' if pages_ok else 'FAIL'}")
    print("\n⚠️ 仍然看不到效果的，以及为什么：")
    print("   - **导入新文件**：要 Celery worker + BGE；导入中心那 16 条 pending 会一直停着。")
    print("   - **FAQ 挖掘**：候选是本脚本直接插的行，`faq_mining` 的**聚类没被这一步验证**。")
    print("     （已发布 FAQ 的向量现在是**真算**的，所以缓存命中这条路是真的。）")
    print("\n📌 前置条件（不满足时对应功能会「静默降级」，不报错）：")
    print("   - BGE：`uv run python backend/scripts/dev_bge_server.py --serve`")
    print("     （另开一个终端；先跑 `--selftest` 确认与库里向量口径一致）")
    print("   - LLM：`.env` 里要有 `DEEPSEEK_API_KEY`（**不是** DEEP_SEEK_KEY，")
    print("     pydantic-settings 对拼错的键静默忽略）。")
    return 0 if (gaps_ok and pages_ok) else 1


async def run() -> int:
    # 🔴 一个脚本**一个**事件循环。`backend/core/db.py` 的 engine 是模块级对象，
    #    连接绑在创建它的事件循环上；写成两次 `asyncio.run()` 会复用上个循环
    #    遗留的连接，报 `AttributeError: 'NoneType' object has no attribute 'send'`
    #    （CLAUDE.md 已知坑，`seed_org.py` 首跑踩过）。
    try:
        return await _run()
    finally:
        milvus_client.close_client()
        await engine.dispose()


USAGE = """\
用法：uv run python -m backend.scripts.seed_demo [选项]

  （无选项）            灌演示数据。⚠️ **不是幂等的** —— FAQ / 会话 / 问答日志
                        每次都新建行（文档/切片/权限/缺口是幂等的）。
                        想重置就 `--clean` 之后再跑，别直接跑第二遍。
  --clean               删掉 demo 前缀的行 + 全部 enabled 文档的权限行，
                        并复位 Milvus ACL。**组织六表与真实文档不动。**
  --refill-faq-vectors  只给缺 question_embedding 的 demo FAQ 补向量，不重灌。
  -h / --help           显示这段说明。
"""

#: 认得的开关。**不在这张表里的一律拒绝**（见 `main_` 的注释）。
_KNOWN_FLAGS = ("--clean", "--refill-faq-vectors")


def main_() -> None:
    argv = list(sys.argv[1:])

    if "-h" in argv or "--help" in argv:
        print(USAGE)
        return

    # 🔴 **不认识的参数一律拒绝，绝不「当作没看见直接跑」。**
    #    原先这里只有 `if "--clean" ... elif "--refill-faq-vectors" ... else: run()`,
    #    于是 `--help` 落进 else 分支 —— **跑了一整遍灌数**。
    #    本脚本没有 argparse，人（和我）很自然会以为 `--help` 是安全的只读操作。
    #    实测踩到：一个 `--help` 把 FAQ 从 6 条变成 18 条、qa_log 从 600 变成 1800，
    #    而且**全程没有任何报错**，看起来就像"命令跑完了"。
    #    这类「未知输入 → 执行最危险的那个分支」是默认分支选错了：
    #    安全的默认是**什么都不做并报错**。
    unknown = [a for a in argv if a not in _KNOWN_FLAGS]
    if unknown:
        print(f"❌ 不认识的参数：{unknown}")
        print()
        print(USAGE)
        sys.exit(2)

    if "--clean" in argv:
        asyncio.run(_clean_and_dispose())
        return
    if "--refill-faq-vectors" in argv:
        sys.exit(asyncio.run(_refill_only_and_dispose()))
    sys.exit(asyncio.run(run()))


async def _refill_only_and_dispose() -> int:
    """只补已发布 FAQ 的 `question_embedding`，**不重灌**任何数据。

    为什么单开这条路：完整重灌要先 `--clean`，而 `--clean` 会删掉 demo 的
    qa_log / qa_session —— 也就是把「你自己在界面上问过的那几轮」一起删掉。
    只想补向量时那个代价太大了，于是宁可多一条窄路径。

    只动 `Faq` 表、只动 `id LIKE 'demo%'` 的行，不碰组织表与真实文档。
    """
    _assert_target_db()
    try:
        async with fresh_session() as db:
            rows = (
                await db.execute(
                    select(Faq).where(
                        Faq.id.like(f"{DEMO}%"), Faq.question_embedding.is_(None)
                    )
                )
            ).scalars().all()
            if not rows:
                print("没有缺向量的 demo FAQ，无需补。")
                return 0
            print(f"待补向量：{len(rows)} 条")
            ok, fail = await _refill_faq_vectors(list(rows))
            await db.commit()
        print(f"完成：补上 {ok} 条，未算出 {fail} 条。")
        return 0 if fail == 0 else 1
    finally:
        milvus_client.close_client()
        await engine.dispose()


async def _clean_and_dispose() -> None:
    try:
        await _clean()
    finally:
        milvus_client.close_client()
        await engine.dispose()


if __name__ == "__main__":
    main_()
