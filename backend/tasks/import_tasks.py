"""知识导入相关异步任务。

链路见 PRD §6.4.1：MinerU 解析 → 结构感知切片 → BGE 向量化 → 写 MySQL + Milvus。
实现在 `backend/engines/import_engine/pipeline.py`；本文件只管
**同步 ↔ 异步的桥接**、**重试策略**与**失败落库**。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.db import fresh_session
from backend.core.exceptions import ParamError, UnsafePayloadError
from backend.core.settings import settings
from backend.models.kb_import_task import KbImportTask
from backend.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 不重试的异常：重试只会得到同样的结果，白白烧掉 MinerU / BGE 的配额。
#: `UnsafePayloadError` = 内容不可用或不安全；`ParamError` = 输入本身不合法。
_PERMANENT = (UnsafePayloadError, ParamError)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _with_session(work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """在一个**新建的** engine 上跑一段协程，收尾 dispose。

    实现已提到 `core/db.py::fresh_session`（be-06 落地时收口）——
    审计清理任务需要同样的语义，两处各写一份时形态漂移不会有任何报错。
    本函数保留原名与原签名，调用点不动。
    """
    async with fresh_session() as session:
        return await work(session)


async def _mark_failed(task_id: str, message: str) -> None:
    """把任务置为失败并记录原因。

    ⚠️ 这一段**自己**处理异常：它是在「已经出错」的路径上跑的，
       如果它再抛，原来的失败原因就被吞掉了，前端只能看到一个
       永远停在 parsing 的任务。宁可丢日志也不丢状态。
    """

    async def _work(session: AsyncSession) -> None:
        task = await session.get(KbImportTask, task_id)
        if task is None:
            logger.error("任务不存在，无法标记失败：%s", task_id)
            return
        task.status = KbImportTask.STATUS_FAILED
        # error_msg 是 VARCHAR(1024)，异常信息可能超长
        task.error_msg = message[:1000]
        task.finished_at = _now()
        await session.commit()

    try:
        await _with_session(_work)
    except Exception:  # noqa: BLE001 —— 见 docstring
        logger.exception("标记任务失败时又出错：task_id=%s", task_id)


def _backoff_seconds(retries: int) -> int:
    """`min(cap, base * factor ** retries)` → **60 / 300 / 900**。

    🔴 **必须自己算，且必须用 `factor=5`。**
       - `retry_backoff` / `retry_backoff_max` 对手动的 `self.retry()` **不生效**，
         它们只被 `celery/app/autoretry.py` 读取（服务于 `autoretry_for=`）。
         不给 `countdown` 的话 Celery 退回 `default_retry_delay=180` 秒。
       - `factor=5` 是为了让序列**正好等于 PRD §6.4.1 的「退避 60/300/900 s」**。
         原先写的 `2 ** retries` 只给 60/120/240，而 `max_retries=3` 决定它
         **永远到不了 300 与 900**（字典 §9 #95 已定案改 5）。
    """
    return min(
        settings.import_retry_backoff_cap,
        settings.import_retry_backoff_base * settings.import_retry_backoff_factor**retries,
    )


@celery_app.task(
    name="import.parse_and_index",
    bind=True,
    max_retries=settings.import_retry_max,
)
def parse_and_index(self, task_id: str) -> dict[str, Any]:  # noqa: ANN001 —— Celery 注入 self
    """解析单个文档并入库。

    重试策略（spec §5.3a）：

    | 类别 | 判定 | 处理 |
    |---|---|---|
    | 可重试 | 网络 / 5xx / 限流 / 轮询超时（`DependencyError`） | 退避 60/300/900 s |
    | 不可重试 | 格式不支持 / 文件损坏 / zip 路径穿越 / 正文为空 | 置 failed，**不重试** |

    返回值只用于排查（`result_backend`），业务状态一律以 `kb_import_task` 为准。
    """
    from backend.engines.import_engine import run_import_pipeline

    logger.info("开始解析任务：%s（第 %d 次尝试）", task_id, self.request.retries + 1)

    async def _work(session: AsyncSession) -> int:
        return await run_import_pipeline(session, task_id)

    try:
        count = asyncio.run(_with_session(_work))
    except _PERMANENT as exc:
        logger.warning("任务永久失败，不重试：task_id=%s reason=%s", task_id, exc)
        asyncio.run(_mark_failed(task_id, str(exc)))
        return {"task_id": task_id, "status": "failed", "retryable": False, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 —— 统一按「可重试」处理
        if self.request.retries >= self.max_retries:
            reason = f"重试 {self.max_retries} 次后仍失败：{exc}"
            logger.error("任务重试耗尽：task_id=%s", task_id, exc_info=True)
            asyncio.run(_mark_failed(task_id, reason))
            return {"task_id": task_id, "status": "failed", "retryable": True, "reason": reason}

        countdown = _backoff_seconds(self.request.retries)
        logger.warning(
            "任务失败，%d 秒后重试（第 %d 次）：task_id=%s err=%s",
            countdown,
            self.request.retries + 2,
            task_id,
            exc,
        )
        # 🔴 countdown 必须显式给，见 _backoff_seconds 的注释
        raise self.retry(exc=exc, countdown=countdown) from exc

    return {"task_id": task_id, "status": "success", "chunks": count}


@celery_app.task(name="import.rebuild_document_index")
def rebuild_document_index(document_id: str) -> dict[str, Any]:
    """正文编辑后全量重建某知识单元的切片与向量（PRD §6.5）。

    不走重试：正文已落库在 `kb_document.source_markdown`，
    失败后管理员再保存一次即可，无需任务级退避。

    ⚠️ 本任务**不建 `kb_import_task` 行**（字典 §5.2 的任务枚举没有
       `rebuild` 这一类，不发明枚举值），因此没有进度可轮询、失败不留记录。
       已登记为待确认项（spec §10）。
    """
    from backend.engines.import_engine import run_rebuild_pipeline

    logger.info("开始重建索引：document_id=%s", document_id)

    async def _work(session: AsyncSession) -> int:
        return await run_rebuild_pipeline(session, document_id)

    try:
        count = asyncio.run(_with_session(_work))
    except Exception as exc:  # noqa: BLE001
        # 这里没有任务行可写，只能留日志 —— 管理员可从「切片数为 0」察觉
        logger.error("索引重建失败：document_id=%s err=%s", document_id, exc, exc_info=True)
        return {"document_id": document_id, "status": "failed", "reason": str(exc)}

    return {"document_id": document_id, "status": "success", "chunks": count}
