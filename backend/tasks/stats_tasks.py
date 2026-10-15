"""运营看板预聚合与清理任务（be-07）。

> 🔴 **本文件是薄壳**。聚合逻辑全在 `service/stats_service.py::aggregate_window`，
> 本文件只负责：起事件循环、开 session、调 service、按需重试。
>
> **方向不能反**：`tasks/` → `service/` 允许（worker 是应用层的调用方）；
> `service/` → `tasks/` 禁止（那是反向依赖，而且必然导致「定时」与「回填」
> 两套实现 —— 而两者输出是同一批列，对不上时没有任何报错）。
> 见 be-07 §2 的 🔧 修订块。

看板只查聚合表，不实时扫 `qa_log` 大表。见 PRD §8.5。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from backend.core.db import fresh_session
from backend.core.settings import settings
from backend.service.stats_service import aggregate_window
from backend.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

__all__ = ["aggregate_hourly", "purge_stats", "backfill_range"]

#: 清理任务单次最多循环多少批。与 `audit_tasks._MAX_BATCHES` 同一理由：
#: 首次上线积压时别一个循环跑到天亮，跑不完下一次 beat 接着删。
_MAX_BATCHES = 200


def _naive_now() -> datetime:
    """naive 的当前时刻（Asia/Shanghai）。

    与 `stats_service._now()` 同口径。**不 import 它**是为了让任务模块
    不依赖 service 的内部私有函数（`_` 前缀就是「别从外面调」的意思），
    这条 8 小时偏移的口径写在两处、但两处都有同样的注释 —— 可接受的重复。
    """
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=8)


@celery_app.task(name="stats.aggregate_hourly")
def aggregate_hourly(lookback_hours: int | None = None) -> dict:
    """把 `qa_log` 汇总进 `stats_hourly` / `stats_daily`。由 beat 每 10 分钟触发。

    ## 为什么窗口是「回溯 lookback_hours」而不是「上一个 10 分钟」

    `qa_log` 的落库与提问时刻之间有**事务提交 + 网络往返 + 连接池排队**，
      会跨桶迟到 —— 一条 09:59:58 发起的提问可能 10:00:03 才落库，
      按「上一桶」算它就永远丢了。

    回溯窗口就是兜这些迟到行的（be-07 §4.2）。而因为整桶**重算 + 覆盖**，
    重复覆盖是**幂等**的：迟到行在下一轮被并入正确的桶，值被改正，
    不会出现「加了两遍」——这正是「重算」相对「累加」的核心优势。

    ## 为什么窗口右端是「当前整点」而不是「当前时刻」

    桶是整点的，`< end` 的右端取整点可以让「当前这个未走完的桶」
    **不被截断**（截断会让本轮写入一个偏小的值，而下一轮再改大 ——
    看板上表现为「今天这个柱子每隔 10 分钟跳一下」，看起来像数据在增长，
    实际是本轮没算全）。取整点后本桶的右端落在整点之后，`< end` 自然覆盖全桶。

    ⚠️ `end` 用 `floor_hour(now) + 1h` 才对：要让**当前**这个桶也进窗口。
       若 `end = floor_hour(now)`，当前桶被 `< end` 排除，看板今日数据会
       永远滞后一小时才出现。
    """
    hours = lookback_hours if lookback_hours is not None else settings.stats_lookback_hours
    now = _naive_now()
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = end - timedelta(hours=hours + 1)

    async def _run() -> dict[str, int]:
        # `fresh_session` 内部新建 engine 并 dispose —— 不能复用模块级 engine
        # （异步 engine 绑在创建它的事件循环上，见 `core/db.py::fresh_session`）。
        async with fresh_session() as db:
            return await aggregate_window(db, start, end)

    summary = asyncio.run(_run())
    summary["start"] = start.isoformat()  # type: ignore[assignment]
    summary["end"] = end.isoformat()  # type: ignore[assignment]
    logger.info("运营看板聚合完成：窗口 [%s, %s) ⇒ %s", start, end, summary)
    return summary


@celery_app.task(name="stats.backfill", bind=True, max_retries=2)
def backfill_range(self, start_iso: str, end_iso: str) -> dict:
    """按区间手工重跑聚合（运维补数用）。

    ⚠️ **不是 HTTP 端点** —— 本期不提供回填接口（字典 §9 #121）。
       回填会重扫任意长的时间区间，把它挂在 HTTP 上等于给了一个
       「一次请求就能让数据库满载」的开关，而收益只是省下一次
       `celery call` 的键盘输入。运维用 Celery 直接投递即可。

    参数是 ISO 字符串而不是 `datetime`：Celery 的消息序列化用 JSON，
    传 `datetime` 依赖 `kombu` 的注册表还原，跨版本行为不一 ——
    传字符串是无歧义的（**naive**，与全链路同一口径）。
    """
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)

    async def _run() -> dict[str, int]:
        async with fresh_session() as db:
            return await aggregate_window(db, start, end)

    try:
        summary = asyncio.run(_run())
    except Exception as exc:
        # 🔴 手动 `self.retry()` **必须显式算退避**（同 `audit_tasks` 的说明）：
        #    `retry_backoff` 对手动重试静默无效，不给 `countdown` 会退回 180 秒。
        countdown = min(
            settings.stats_purge_retry_backoff_cap,
            settings.stats_purge_retry_backoff_base * 2**self.request.retries,
        )
        logger.warning("看板回填失败，%s 秒后重试：%s", countdown, exc)
        raise self.retry(exc=exc, countdown=countdown) from exc

    logger.info("看板回填完成：[%s, %s) ⇒ %s", start, end, summary)
    return summary


@celery_app.task(name="stats.purge_stats", bind=True, max_retries=3)
def purge_stats(self, retention_days: int | None = None) -> dict:
    """清理过期的小时桶。日桶按配置可永久保留。

    🔴 **只清 `stats_hourly`，默认不碰 `stats_daily`。**
       日表是**长期趋势**的唯一来源（小时表只有 90 天），删了就没法回溯了；
       而小时表只服务「近 48h」这类细粒度查询，老桶确实没用。

    🔴 **`stats_daily_retention_days = 0` 表示「永久保留」，必须显式判断。**
       写成 `WHERE bucket_date < now - INTERVAL :days DAY` 且 `days = 0` ——
       那会删掉**今天之前的全部日数据**，即整个历史趋势。
       这个参数的默认值恰好是 0，所以这条路是**默认路径**，不是边界情况。

    分批删除的理由同 `audit_tasks`：一次 `DELETE` 几十万行会长时间持行锁 +
    生成巨大 binlog 事务，症状是「凌晨那个时刻全站写操作变慢」。
    """
    days = retention_days if retention_days is not None else settings.stats_hourly_retention_days
    cutoff = _naive_now() - timedelta(days=days)
    batch = settings.stats_purge_batch_size
    daily_days = settings.stats_daily_retention_days

    async def _run() -> dict[str, int]:
        async with fresh_session() as db:
            total = 0
            batches = 0
            for _ in range(_MAX_BATCHES):
                # 参数化（安全红线 8）：`LIMIT` 也不拼字符串。
                result = await db.execute(
                    text(
                        "DELETE FROM stats_hourly WHERE bucket_start < :cutoff LIMIT :batch"
                    ),
                    {"cutoff": cutoff, "batch": batch},
                )
                await db.commit()
                affected = result.rowcount or 0
                total += affected
                batches += 1
                if affected < batch:
                    break

            daily_deleted = 0
            # 🔴 0 = 永久保留 ⇒ **完全跳过日表**，不构造任何 DELETE。
            #    用 `if daily_days > 0` 而不是 `if daily_days:` —— 显式表达
            #    「0 是有意义的值（永久）」，而不是「0 是假值所以跳过」。
            if daily_days > 0:
                daily_cutoff = _naive_now() - timedelta(days=daily_days)
                result = await db.execute(
                    text(
                        "DELETE FROM stats_daily WHERE bucket_date < :cutoff LIMIT :batch"
                    ),
                    {"cutoff": daily_cutoff.date(), "batch": batch},
                )
                await db.commit()
                daily_deleted = result.rowcount or 0

            return {"hourly_deleted": total, "batches": batches, "daily_deleted": daily_deleted}

    try:
        summary = asyncio.run(_run())
    except Exception as exc:
        countdown = min(
            settings.stats_purge_retry_backoff_cap,
            settings.stats_purge_retry_backoff_base * 2**self.request.retries,
        )
        logger.warning("看板清理失败，%s 秒后重试：%s", countdown, exc)
        raise self.retry(exc=exc, countdown=countdown) from exc

    logger.info(
        "看板清理完成：小时桶 %s 行、日桶 %s 行，小时桶截止 %s（小时保留 %s 天 / 日表 %s）",
        summary["hourly_deleted"],
        summary["daily_deleted"],
        cutoff,
        days,
        "永久保留" if daily_days == 0 else f"{daily_days} 天",
    )
    summary["cutoff"] = cutoff.isoformat()  # type: ignore[assignment]
    return summary
