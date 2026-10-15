"""知识沉淀挖掘相关异步任务（be-08 FAQ 聚类 / be-09 缺口聚合）。

> 🔴 **本文件是薄壳**。聚类逻辑全在 `engines/data_mining_engine/faq_mining.py`，
>   本文件只负责：起事件循环、开 session、调引擎、按需重试。
>
> **方向不能反**：`tasks/` → `engines/` 允许（worker 是调用方）；
>   `engines/` → `tasks/` 禁止 —— 反向依赖会让「定时跑」与「手动触发」
>   各有一套逻辑，而两者产出的是同一批候选，对不上时没有任何报错。
>   同 `stats_tasks.py` 的取舍。

链路见 PRD §6.3：日志聚类 → 候选 FAQ 生成（be-08）/ 知识缺口聚合（be-09）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from backend.core.db import fresh_session
from backend.engines.data_mining_engine import faq_mining
from backend.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

__all__ = ["aggregate_knowledge_gaps", "mine_faq_clusters"]

#: 重试上限。挖掘是「错过这一次，下一次补得上」的任务（滑动窗口 + 去重池），
#: 所以不需要很多次重试 —— 3 次足够覆盖数据库短暂抖动。
_MAX_RETRIES = 3
#: 退避基数（秒）。见下面对 `countdown` 的说明。
_RETRY_BASE = 60
#: 退避上限（秒）。
_RETRY_CAP = 600


@celery_app.task(bind=True, name="mining.mine_faq_clusters", max_retries=_MAX_RETRIES)
def mine_faq_clusters(self, lookback_days: int | None = None) -> dict:
    """聚类历史提问，生成候选 FAQ。由 beat 每日 02:37 触发。

    也可由 `POST /api/mining/faq/mine` 同步调用（那条路径**不走本任务**，
    见 be-08 §5.6 —— 手动挖掘在路由内直接调引擎，因为前端要的是结果不是任务号）。

    ## 为什么自己 `asyncio.run` 而不是在 Celery 里挂异步

    Celery 的 worker 是同步的。挂异步要额外的池适配器，而本项目已有的两个
    任务模块（`import_tasks` / `stats_tasks`）都是这个写法 —— 保持一致，
    且 `fresh_session()` 的「一个任务一个事件循环」前提正是为这个写法准备的。
    """
    try:
        return asyncio.run(_run(lookback_days))
    except Exception as exc:  # noqa: BLE001 —— 统一走 Celery 重试
        logger.exception("FAQ 聚类挖掘失败（第 %d 次）", self.request.retries + 1)
        # 🔴 **必须显式算退避**：`retry_backoff` / `retry_backoff_max` 这两个
        #    Celery 配置对手动的 `self.retry()` **不生效**、会被静默忽略，
        #    不给 `countdown` 就退回 `default_retry_delay = 180` 秒
        #    （CLAUDE.md「已知坑 / Celery」第 1 条，实测 5.6.3）。
        countdown = min(_RETRY_CAP, _RETRY_BASE * 2**self.request.retries)
        raise self.retry(exc=exc, countdown=countdown) from exc


async def _run(lookback_days: int | None) -> dict:
    """真正干活的部分。每次调用**自建 session**（见 `fresh_session` 的说明）。"""
    from datetime import datetime, timedelta

    from backend.core.settings import settings

    days = lookback_days if lookback_days is not None else settings.mining_lookback_days
    now = datetime.now()
    async with fresh_session() as db:
        # ⚠️ 复用引擎的**同一个函数**，不在这里另写一遍窗口计算 ——
        # 手动挖掘与定时挖掘必须挖同样的窗口，否则「手动点一下有 12 条、
        # 晚上自动跑出来 3 条」，而两边的代码各自看都对。
        return await faq_mining.mine_faq_clusters(
            db, start=now - timedelta(days=days), end=now
        )


@celery_app.task(bind=True, name="mining.aggregate_knowledge_gaps", max_retries=_MAX_RETRIES)
def aggregate_knowledge_gaps(self, since: str | None = None, until: str | None = None) -> dict:
    """聚合未命中提问为知识缺口项，并自动关闭已能作答的 `tasked` 缺口。

    由 beat 每 10 分钟触发（`aggregate-knowledge-gaps`）。

    🔴 **`bind=True` 是必需的**（📄 壳子原本没有）：下面的 `self.retry()`
       要拿 `self`，少了它这里会直接 `TypeError`。

    ⚠️ `since` / `until` 是可选的 ISO 字符串，**只给排障用**（手工回填某一段窗口）。
       常态路径不传，走 `GAP_AGGREGATE_LOOKBACK_HOURS`。
    """
    try:
        return asyncio.run(_run_gaps(since, until))
    except Exception as exc:  # noqa: BLE001 —— 统一走 Celery 重试
        logger.exception("缺口聚合失败（第 %d 次）", self.request.retries + 1)
        # 🔴 **必须显式算退避**：`retry_backoff` / `retry_backoff_max` 对手动的
        #    `self.retry()` **不生效**、会被静默忽略，不给 `countdown` 就退回
        #    `default_retry_delay = 180` 秒（CLAUDE.md「已知坑 / Celery」第 1 条）。
        countdown = min(_RETRY_CAP, _RETRY_BASE * 2**self.request.retries)
        raise self.retry(exc=exc, countdown=countdown) from exc


async def _run_gaps(since: str | None, until: str | None) -> dict:
    """真正干活的部分。每次调用**自建 session**（见 `fresh_session` 的说明）。"""
    from datetime import timedelta

    from backend.core.settings import settings
    from backend.engines.data_mining_engine import gap

    now = datetime.now()
    end = _parse_ts(until) or now
    start = _parse_ts(since) or (end - timedelta(hours=settings.gap_aggregate_lookback_hours))
    if start >= end:
        # 窗口反了就是**没有可聚合的东西**，而不是「把全表聚合一遍」。
        # 静默返回 0 比抛异常合适：这是运维手工传参的错误，不是数据问题。
        logger.warning("缺口聚合窗口非法（since=%s >= until=%s），本轮跳过", start, end)
        return {"scanned": 0, "created": 0, "updated": 0, "closed": 0, "skipped_by_acl": 0,
                "window": {"since": start.isoformat(), "until": end.isoformat()}}
    async with fresh_session() as db:
        result = await gap.aggregate_gaps(db, since=start, until=end)
    return result.as_dict()


def _parse_ts(raw: str | None) -> datetime | None:
    """ISO 字符串 → `datetime`；空 / 解析失败返回 `None`（退回默认窗口）。

    **解析失败不抛**：这两个参数只服务排障，传错了就用默认窗口并留一条 warning，
    比让整轮定时聚合因为一个手输的参数而失败要好。
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("缺口聚合的窗口参数无法解析，已退回默认窗口：%r", raw)
        return None
