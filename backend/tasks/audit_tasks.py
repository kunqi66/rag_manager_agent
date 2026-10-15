"""审计保留期清理（be-06）。

审计表**只增不减**是它的语义，但**无限增长**不是 —— 审计页默认只查最近 7 天，
老数据对查询无用（对合规才有用），留着只会让 `(created_at)` 索引变大、
`DELETE` 变慢，最终拖垮那条主路径。

🔴 **必须分批删**（`AUDIT_PURGE_BATCH_SIZE`）。一次 `DELETE` 几十万行会：
- 长时间持有行锁 → 清理期间所有审计写入排队；
- 生成巨大的 binlog 事务 → 主从延迟。

症状是「凌晨那个时刻全站写操作变慢」，与审计毫无表面关联，极难反查。
分批后每批是一个小事务，锁的持有时间与批量成正比下降。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.db import fresh_session
from backend.core.settings import settings
from backend.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

__all__ = ["purge_audit_logs"]

#: 单次任务最多循环多少批。防的是「首次上线时积压了几百万行，
#: 一个批次循环跑到天亮」。跑不完不要紧，下一次 beat 接着删。
_MAX_BATCHES = 200


async def _purge(db: AsyncSession, cutoff: datetime, batch: int) -> tuple[int, int]:
    """循环分批删，返回 (删除行数, 批次数)。"""
    total = 0
    batches = 0
    for _ in range(_MAX_BATCHES):
        # 🔴 参数化（安全红线 8）：`LIMIT` 也不拼字符串。
        #    MySQL 的 DELETE 支持 LIMIT，这是「分批」能成立的前提 ——
        #    换成 `WHERE id IN (SELECT ... LIMIT n)` 会先物化整个子查询，
        #    分批反而更慢。
        result = await db.execute(
            text("DELETE FROM sys_audit_log WHERE created_at < :cutoff LIMIT :batch"),
            {"cutoff": cutoff, "batch": batch},
        )
        await db.commit()
        affected = result.rowcount or 0
        total += affected
        batches += 1
        if affected < batch:
            # 最后一批没满 = 已删干净，不必再发一次空查询。
            break
    return total, batches


@celery_app.task(name="audit.purge_audit_logs", bind=True, max_retries=3)
def purge_audit_logs(self, retention_days: int | None = None) -> dict:
    """删除超过保留期的审计行，返回删除统计。

    `retention_days` 可覆盖配置，仅用于运维手工补删历史积压；定时调度不传。
    """
    days = retention_days if retention_days is not None else settings.audit_retention_days
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    batch = settings.audit_purge_batch_size

    async def _run() -> tuple[int, int]:
        # `fresh_session` 内部新建 engine 并 dispose —— 不能复用模块级 engine
        # （异步 engine 绑在创建它的事件循环上，见 `core/db.py::fresh_session`）。
        async with fresh_session() as db:
            return await _purge(db, cutoff, batch)

    try:
        deleted, batches = asyncio.run(_run())
    except Exception as exc:
        # 🔴 手动 `self.retry()` **必须显式算退避**：`retry_backoff` /
        #    `retry_backoff_max` 只被 `autoretry_for=` 的声明式重试读取，
        #    对手动 retry 静默无效，不给 `countdown` 会退回
        #    `default_retry_delay = 180` 秒（CLAUDE.md 已知坑）。
        countdown = min(
            settings.audit_purge_retry_backoff_cap,
            settings.audit_purge_retry_backoff_base * 2**self.request.retries,
        )
        logger.warning("审计清理失败，%s 秒后重试：%s", countdown, exc)
        raise self.retry(exc=exc, countdown=countdown) from exc

    logger.info("审计清理完成：删除 %s 行（%s 批），截止 %s", deleted, batches, cutoff)
    return {"deleted": deleted, "batches": batches, "cutoff": cutoff.isoformat()}
