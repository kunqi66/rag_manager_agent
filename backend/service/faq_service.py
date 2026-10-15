"""FAQ 生命周期服务（be-08）。

三件事：

1. **进程内索引的生命周期** —— 启动装载、Redis 订阅增量刷新、定时兜底全量重载；
2. **已发布 FAQ 的读** —— 列表分页；
3. **关联文档标题的权限回填** —— 候选列表要显示「关联知识单元」，而标题属
   数据权限约束的内容（红线 4）。

🔴 **索引刷新为什么必须广播**（be-08 §5.4）：API 是 `uvicorn` 多 worker、
   worker 是 Celery 多进程，**每个进程一份索引**。发布一条 FAQ 后只刷新当前进程，
   其余进程直到重启都命中不了它 —— 而且不报错，表现是「刚发布的 FAQ 时灵时不灵」。
   刷新走 Redis pub/sub（频道 `faq:changed`），另加一个定时全量重载兜底
   （订阅可能掉线，掉线是静默的）。

🔴 **本模块不写业务 SQL 之外的东西**：聚类在 `engines/data_mining_engine/faq_mining`，
   本文件只做「读 + 索引维护 + 权限回填」。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.clients import redis_client
from backend.core.exceptions import NotFoundError
from backend.core.settings import settings
from backend.engines.import_engine import list_visible_document_ids
from backend.engines.search_engine.acl import post_verify
from backend.faq.faq_cache import CHANGED_CHANNEL, FaqHit, faq_cache
from backend.models.faq import Faq
from backend.schemas.auth import AuthContext
from backend.schemas.mining import FaqItemDTO, FaqQuery, RelatedDocBrief
from backend.service import audit_service

logger = logging.getLogger(__name__)

__all__ = [
    "apply_change",
    "attach_related_docs",
    "count_published",
    "list_faqs",
    "load_cache_from_db",
    "match_faq",
    "publish_change",
    "set_cache_enabled",
    "start_background_tasks",
    "startup",
    "stop_background_tasks",
]


# ═══════════════════════════════════════════════════════════════
# 索引装载与刷新
# ═══════════════════════════════════════════════════════════════


async def load_cache_from_db(db: AsyncSession) -> int:
    """从 MySQL 全量重建索引。返回装载条数。

    只取 `cache_enabled = true` 且**向量非空**的行 —— 过滤在 SQL 里做，
    `FaqCache.load_all` 因此不需要知道开关的存在（单一职责）。
    走 `ix_faq_cache_enabled`。

    ⚠️ `question_embedding IS NOT NULL` 这个条件不能省：向量为空的行的
       `cache_enabled` 仍是 `true`（发布时 embedding 调用失败会走到这），
       传进去只会被 `from_blob` 逐条跳过并刷一屏 warning，把真正的脏数据告警淹掉。
    """
    stmt = select(
        Faq.id, Faq.question, Faq.answer, Faq.question_embedding
    ).where(Faq.cache_enabled.is_(True), Faq.question_embedding.is_not(None))
    rows = (await db.execute(stmt)).all()
    return faq_cache.load_all([(row[0], row[1], row[2], row[3]) for row in rows])


async def publish_change(op: str, faq_id: str) -> None:
    """广播一次索引变更。`op` ∈ `{"upsert", "delete"}`。

    ⚠️ **广播失败不抛异常**，只告警：发布 FAQ 已经落库成功了，
       因为「通知别人刷新缓存」这一步失败而让整个发布接口报错，
       会把一次成功的业务操作变成失败。掉线由定时全量重载兜底
       （最坏 `FAQ_CACHE_RELOAD_INTERVAL_SECONDS` 秒后一致）。
    """
    try:
        client = redis_client.get_client()
        await redis_client.guard(
            "PUBLISH", client.publish(CHANGED_CHANNEL, json.dumps({"op": op, "faq_id": faq_id}))
        )
    except Exception:  # noqa: BLE001 —— 广播是尽力而为，任何失败都不该冒泡
        logger.warning("FAQ 变更广播失败（faq_id=%s op=%s），将由定时重载兜底", faq_id, op)


async def apply_change(db: AsyncSession, op: str, faq_id: str) -> None:
    """按广播消息刷新**当前进程**的索引。"""
    if op == "delete":
        faq_cache.remove(faq_id)
        return
    # upsert：回库里取权威内容。**不从消息里带内容** —— 消息可能乱序、
    # 也可能是两次快速修改，回库读能保证装进索引的一定是最新那一版。
    row = (
        await db.execute(
            select(
                Faq.id, Faq.question, Faq.answer, Faq.question_embedding, Faq.cache_enabled
            ).where(Faq.id == faq_id)
        )
    ).first()
    if row is None or not row[4]:
        # 已删 / 已停用缓存 —— 都当删除处理。
        # 停用时**必须**移出索引：留在里面等于开关没生效，而开关是运营
        # 用来「这条 FAQ 答得不对，先别用」的唯一手段。
        faq_cache.remove(faq_id)
        return
    faq_cache.upsert(row[0], row[1], row[2], row[3])


async def _reload_forever() -> None:
    """兜底：每 `FAQ_CACHE_RELOAD_INTERVAL_SECONDS` 秒全量重载一次。

    ⚠️ **每条路径都要各自 try/except**：一次重载失败（比如 MySQL 抖动）
       如果让这个协程退出，就再也没人重载了 —— 而退出是静默的，
       表现是「服务跑久了新 FAQ 就命中不了」，重启即可恢复，
       于是永远查不到根因。
    """
    while True:
        await asyncio.sleep(settings.faq_cache_reload_interval_seconds)
        try:
            # 这里必须自建 session：本协程的生命周期与请求无关，
            # 没有 `Depends(get_db)` 可注入，也不能长期占着一条请求连接。
            from backend.core.db import SessionLocal

            async with SessionLocal() as db:
                count = await load_cache_from_db(db)
            logger.info("FAQ 索引定时重载完成，共 %d 条", count)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 见上：绝不能让循环退出
            logger.exception("FAQ 索引定时重载失败，将在下个周期重试")


async def _subscribe_forever() -> None:
    """订阅 `faq:changed`，收到即刷新本进程索引。"""
    while True:
        client = redis_client.get_client()
        pubsub = client.pubsub()
        try:
            await redis_client.guard("SUBSCRIBE", pubsub.subscribe(CHANGED_CHANNEL))
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                    op, faq_id = payload.get("op"), payload.get("faq_id")
                    if op not in {"upsert", "delete"} or not faq_id:
                        continue
                    from backend.core.db import SessionLocal

                    async with SessionLocal() as db:
                        await apply_change(db, op, faq_id)
                except Exception:  # noqa: BLE001 —— 一条消息处理失败不能断订阅
                    logger.exception("处理 FAQ 变更消息失败")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # 订阅断线（Redis 重启 / 网络抖动）。**必须重连**：
            # 不重连的话进程从此收不到变更，只能靠定时重载，
            # 而定时重载的间隔在配置里 —— 于是「改大间隔」会顺带拉长这个窗口。
            logger.exception("FAQ 变更订阅中断，5 秒后重连")
            await asyncio.sleep(5)
        finally:
            # 不关掉 pubsub 会攒下连接，直到打满 Redis 的 maxclients。
            with contextlib.suppress(Exception):
                await pubsub.aclose()


_tasks: list[asyncio.Task[None]] = []


def start_background_tasks() -> None:
    """由 `main.py` 的 `lifespan` 调用。

    ⚠️ 用 **asyncio 任务**而不是「后台线程 + 线程内的新事件循环」：
       `redis.asyncio` 的客户端与连接池绑定在**创建它的事件循环**上，
       跨循环使用会报 `AttributeError: 'NoneType' object has no attribute 'send'`
       （CLAUDE.md 记过同款的 async engine 跨 `asyncio.run()` 问题）。
       同循环内起任务没有这个问题，也省掉一整套线程间同步。
    """
    if _tasks:
        return
    _tasks.append(asyncio.create_task(_subscribe_forever(), name="faq-cache-subscriber"))
    _tasks.append(asyncio.create_task(_reload_forever(), name="faq-cache-reloader"))


async def stop_background_tasks() -> None:
    """由 `lifespan` 的关闭段调用。

    ⚠️ 不取消的话，`uvicorn --reload` 每次重载都会多留两个协程，
       表现为「改几次代码后 Redis 连接数一直涨」。
    """
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    _tasks.clear()


async def startup() -> None:
    """启动装载 + 起后台任务。

    🔴 **装载失败不能阻断启动**：`faq_cache` 空着的后果是「FAQ 命中不了、
       走正常检索」，这是**降级**不是故障。为了一个可降级的能力让整个 API
       起不来（比如启动瞬间 MySQL 抖动），代价与收益完全不成比例。
    """
    try:
        from backend.core.db import SessionLocal

        async with SessionLocal() as db:
            count = await load_cache_from_db(db)
        logger.info("FAQ 索引启动装载完成，共 %d 条", count)
    except Exception:  # noqa: BLE001 —— 见 docstring
        logger.exception("FAQ 索引启动装载失败，本次启动不带 FAQ 缓存（将由定时重载补齐）")
    start_background_tasks()


# ═══════════════════════════════════════════════════════════════
# 读
# ═══════════════════════════════════════════════════════════════


def match_faq(query_vector) -> FaqHit | None:  # noqa: ANN001 —— ndarray
    """给 `engines/search_engine/qa.py` 用的**薄封装**。

    存在的理由：`qa.py` 直接 import `faq_cache` 单例也行，但那样
    「谁负责装载」就没人知道了。经这层之后，问答路径只知道
    「问 faq_service 有没有命中」，装载与刷新全在 service 内闭环。
    """
    return faq_cache.match(query_vector)


def _to_item(row: Faq) -> FaqItemDTO:
    return FaqItemDTO(
        id=row.id,
        question=row.question,
        answer=row.answer,
        cache_enabled=bool(row.cache_enabled),
        source_candidate_id=row.source_candidate_id,
        published_by=row.published_by,
        published_at=row.published_at,
        hit_count=row.hit_count,
    )


async def list_faqs(db: AsyncSession, query: FaqQuery) -> tuple[list[FaqItemDTO], int]:
    """已发布 FAQ 的分页列表。返回 `(当页数据, 总数)`。

    分页**两次查询**（count + 当页）而不是只查一次再在内存里切：
    后者要先把全表读进内存，FAQ 上千条之后就是每次翻页全表扫。
    """
    conditions = []
    if query.keyword:
        # ⚠️ `%...%` 子串匹配是**参数化**的（`like` 的右侧走绑定参数），
        #    不拼接字符串，红线 8 无碍。
        #    不用 `LIKE 'prefix%'`：CLAUDE.md 记过 id 里的 `_` 是 LIKE 通配符，
        #    虽然这里匹配的是 `question`（用户输入，不是 id），
        #    但同一个坑不值得再踩一次 —— 子串匹配本来也不需要前缀语义。
        conditions.append(Faq.question.like(f"%{query.keyword}%"))

    total = await db.scalar(select(func.count()).select_from(Faq).where(*conditions)) or 0
    stmt = (
        select(Faq)
        .where(*conditions)
        # 排序必须**稳定且唯一**：只按 `published_at` 排，同一秒发布的两条
        # 在不同页的查询里可能换位，表现为「翻页看到重复的一条、另一条不见了」。
        # 加 `id` 做次级键即可消除。
        .order_by(Faq.published_at.desc(), Faq.id.desc())
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    )
    rows = (await db.scalars(stmt)).all()
    return [_to_item(row) for row in rows], int(total)


async def set_cache_enabled(
    db: AsyncSession, faq_id: str, enabled: bool, ctx: AuthContext
) -> FaqItemDTO:
    """开关单条 FAQ 的快速应答缓存，并同步本进程索引。

    抛 `NotFoundError`（3001）当 FAQ 不存在 —— **不能静默成功**：
    前端会把它当成「已生效」，而实际上什么都没发生。

    🔴 **这个开关必须留痕**（be-06 §5.4 的 `faq.cache.toggle`）：它是运营
       用来「这条答得不对，先别用」的唯一手段，关掉之后该问题会退回正常
       检索链路。也就是说「缓存是被谁、在什么时候关掉的」直接决定了一次
       答非所问能不能被解释 —— 而这一行只在审计表里。
    """
    row = await db.get(Faq, faq_id)
    if row is None:
        raise NotFoundError("FAQ 不存在")

    if row.cache_enabled != enabled:
        # 只在真的变化时才写：重复点同一次不产生无意义的 binlog，
        # 也避免「开关没动却触发了全量广播」。
        row.cache_enabled = enabled
        # 留痕搭本次事务（`record()` 自己不 commit），且与上面的判断同一个条件：
        # 没变化就不记 —— 否则运营误点几下就会在审计表里留下一串
        # 「把已经关着的开关又关了一次」，把真正的开关动作淹掉。
        # ⚠️ **不要往 detail 里加「旧值」**：这条只在**真的变了**的时候落，
        #    所以旧值恒等于 `not enabled`，加了是冗余；而且该动作的键集在
        #    `audit_service` 里是按 action 冻结的，多传的键会被 `sanitize_detail`
        #    **静默丢掉**（不报错）—— 写了却不存在，比不写更容易骗到人。
        audit_service.record(
            db,
            ctx,
            audit_service.AuditAction.FAQ_CACHE_TOGGLE,
            target_type=audit_service.AuditTargetType.FAQ,
            target_id=faq_id,
            detail={"faq_id": faq_id, "cache_enabled": enabled},
        )
        await db.commit()
        # ⚠️ commit 之后要 refresh：`updated_at` 之类由库里生成的值，
        #    不 refresh 的话返回体是旧值（CLAUDE.md 记过这个坑）。
        #    本表的 `cache_enabled` 是我们自己写的，但 `FaqItemDTO` 里
        #    还有 `hit_count` 等列，统一 refresh 一遍更稳。
        await db.refresh(row)

    # 本进程：**立即生效**。广播是给别的进程的，自己等广播回来
    # 会有一个「刚点完开关、立刻提问仍然命中缓存」的窗口。
    if enabled:
        faq_cache.upsert(row.id, row.question, row.answer, row.question_embedding)
    else:
        faq_cache.remove(row.id)

    # 别的进程：广播。失败只告警（由定时重载兜底），见 `publish_change`。
    await publish_change("upsert" if enabled else "delete", faq_id)
    return _to_item(row)


# ═══════════════════════════════════════════════════════════════
# 权限回填
# ═══════════════════════════════════════════════════════════════


async def attach_related_docs(
    db: AsyncSession, ctx: AuthContext, doc_ids: list[str]
) -> list[RelatedDocBrief]:
    """把 `document_id` 列表换成带标题的精简信息，**无权看的整条剔除**。

    🔴 红线 4：「未授权内容一律不返回正文、不返回标题」。
       候选 FAQ 的「关联知识单元」是运营判断「这条该不该发」的主要依据，
       但它不能成为绕过数据权限看标题的口子 —— 候选列表的读者
       （`kb_admin`）未必对每个部门的文档都有数据权限。

    🔴 **必须复用 `search_engine.acl.post_verify`，不新写一份判定**。
       「谁能看某文档」这件事全项目只有一处实现（四维 OR + 可见集合），
       另写一份的后果是两处判定在某天分叉 —— 表现为「检索时说无权、
       候选列表里却看得到标题」，而这是**越权**。
       `post_verify` 顺带把标题也查了，正好是我们要的东西。

    ⚠️ **整条剔除而不是把 `title` 置空**：置空会在前端留一个空框，
       等于确认「这里有一篇你看不到的文档」（be-06 的同款取舍）。
    """
    wanted = [doc_id for doc_id in dict.fromkeys(doc_ids) if doc_id]
    if not wanted:
        return []

    visible = await list_visible_document_ids(db)
    result = await post_verify(db, set(wanted), ctx, visible)

    # 按入参顺序输出：`post_verify` 返回的是 set，迭代序不稳定，
    # 直接用它会让候选卡片上的关联文档每次刷新都换顺序。
    briefs: list[RelatedDocBrief] = []
    for doc_id in wanted:
        if doc_id not in result.allowed:
            continue
        briefs.append(RelatedDocBrief(id=doc_id, title=result.titles.get(doc_id, doc_id)))
    if result.blocked:
        logger.info(
            "候选关联文档剔除 %d/%d 条（当前用户无数据权限）",
            len(result.blocked),
            len(wanted),
        )
    return briefs


async def count_published(db: AsyncSession) -> int:
    """已发布 FAQ 总数。候选审核页的统计块与验收断言用。"""
    return int(await db.scalar(select(func.count()).select_from(Faq)) or 0)
