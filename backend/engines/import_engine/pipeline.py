"""解析入库流水线（worker 侧，spec §5.2）。

与 `lifecycle.py` 的分界是**运行环境**，不是业务领域：
本文件跑在 Celery worker 进程里，lifecycle 跑在 API 进程。

```
解析（拿 markdown）
  → 🔴 回写 source_markdown
  → 结构感知切片
  → BGE 批量向量化
  → 双写 MySQL kb_chunk + Milvus kb_chunk
  → 回写 chunk_count、任务置 success
```

🔴 **回写 `source_markdown` 的位置是关键**（字典 §9 #94）。
   必须在「解析一成功」就落库，而不是等整条链路成功。
   原因：原始文件在本项目里**没有任何持久化位置**（`kb_document` 没有文件字段、
   MinIO 不在本模块设计内、compose 里 api 与 worker 也没有共享卷）。
   若留到最后才写，则「解析成功但切片/向量化失败」时 markdown 从未落库，
   重试拿不到任何输入 —— 只能反复失败。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.clients import embedding_client, milvus_client, mineru_client
from backend.core.exceptions import DependencyError, NotFoundError, UnsafePayloadError
from backend.core.ids import new_id
from backend.engines.import_engine.chunking import split_markdown
from backend.engines.import_engine.lifecycle import derive_acl
from backend.models.kb_chunk import KbChunk
from backend.models.kb_document import KbDocument, KbDocumentPermission
from backend.models.kb_import_task import KbImportTask

logger = logging.getLogger(__name__)

__all__ = ["run_import_pipeline", "run_rebuild_pipeline"]

_CHUNK_PREFIX = "ck"

#: 进度里程碑（0–100）。给前端进度条用，数值本身无语义，只要单调递增。
_PROGRESS_PARSED = 40
_PROGRESS_CHUNKED = 60
_PROGRESS_EMBEDDED = 85
_PROGRESS_DONE = 100


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _set_task_progress(
    db: AsyncSession,
    task: KbImportTask,
    status: str,
    progress: int,
    **extra: Any,
) -> None:
    """推进任务状态并落库。

    🔴 **每一步都要落库**，不能攒到最后一起写：前端是轮询
       `GET /tasks/{id}` 拿进度的，只在成功/失败时写一次的话，
       进度条会在整个解析期间停在 0 —— 用户以为卡死了。
    """
    task.status = status
    task.progress = progress
    for key, value in extra.items():
        setattr(task, key, value)
    await db.commit()


async def _resolve_markdown(
    db: AsyncSession, doc: KbDocument, task: KbImportTask
) -> str:
    """取得待切片的 Markdown，并**立刻回写** `source_markdown`（§9 #94）。

    两条来源，按优先级：
    1. `kb_document.source_markdown` 已有 → 直接复用（重试走这条，不再调 MinerU）；
    2. `kb_import_task.mineru_task_id` 有 → 凭 job_id 取回结果（受理时已提交）。
    """
    if doc.source_markdown:
        logger.info("复用已落库的 source_markdown：document_id=%s", doc.id)
        return doc.source_markdown

    if not task.mineru_task_id:
        raise UnsafePayloadError("任务没有可用的解析输入，请重新上传")

    markdown = await mineru_client.fetch_markdown(
        task.mineru_task_id, task.file_name or ""
    )
    if not markdown.strip():
        raise UnsafePayloadError("MinerU 返回的正文为空")

    # 🔴 这一写就是「解析成功」的落库点，必须在切片之前
    doc.source_markdown = markdown
    await db.commit()
    logger.info("source_markdown 已落库：document_id=%s（%d 字符）", doc.id, len(markdown))
    return markdown


async def _load_permission_rows(
    db: AsyncSession, document_id: str
) -> list[KbDocumentPermission]:
    stmt = select(KbDocumentPermission).where(
        KbDocumentPermission.document_id == document_id
    )
    return list((await db.scalars(stmt)).all())


async def _index_document(db: AsyncSession, doc: KbDocument, markdown: str) -> int:
    """切片 → 向量化 → 双写。返回切片数。

    写入顺序是**先 MySQL 后 Milvus**（spec §5.5）：MySQL 是权威，
    先落权威再落副本 —— 反过来的话，Milvus 写成功而 MySQL 失败会留下
    「检索得到但库中没有」的幽灵切片，而 be-05 的后置兜底正是拿 MySQL 复核的，
    那种片子会被兜底剔除，等于白写。
    """
    drafts = split_markdown(markdown)
    if not drafts:
        # 空正文切不出任何片。写成一片空 content 是更坏的选择 ——
        # 空向量的相似度表现异常，会以中等分数命中**所有**查询。
        raise UnsafePayloadError("解析结果为空，无法切片")

    vectors = await embedding_client.embed_texts([d.content for d in drafts])
    if len(vectors) != len(drafts):
        raise DependencyError("向量条数与切片数不符")

    # —— MySQL 权威 ——
    # 🔴 id 在这里**先生成好**，MySQL 行与 Milvus 行共用同一批。
    #    不要写成「先插入再从 session 里把 id 捞回来」——那样一旦取错，
    #    表现是「引用卡片指向另一篇文档」，而且不报任何错。
    chunk_rows = [
        KbChunk(
            id=new_id(_CHUNK_PREFIX),
            document_id=doc.id,
            chunk_index=draft.chunk_index,
            content=draft.content,
            heading_path=draft.heading_path,
            token_count=draft.token_count,
        )
        for draft in drafts
    ]
    await db.execute(delete(KbChunk).where(KbChunk.document_id == doc.id))
    db.add_all(chunk_rows)
    await db.commit()

    # —— Milvus 副本：id 与上面**完全一致**，后续按 id 取回 MySQL 正文 ——
    # ACL 每次写入时**重新派生**，不是上传时算死（spec §5.5）。
    # 新导入的文档通常还没有权限行 → 四个字段全空 → 只有知识管理员看得到，
    # 这是字典 §2.2 的判定规则，**不是 bug**。
    acl = derive_acl(await _load_permission_rows(db, doc.id))
    rows = [
        {
            "id": chunk.id,
            "document_id": doc.id,
            "content": draft.content,
            "heading_path": draft.heading_path or "",
            "dense": vector,
        }
        for chunk, draft, vector in zip(chunk_rows, drafts, vectors, strict=True)
    ]

    await milvus_client.upsert_chunks(rows, acl)
    return len(drafts)


async def run_import_pipeline(db: AsyncSession, task_id: str) -> int:
    """执行一次导入任务，返回切片数。

    抛出的异常由 `backend/tasks/import_tasks.py` 决定「重试还是永久失败」：
    - `UnsafePayloadError` / `ParamError` → 永久失败（重试结果必然相同）；
    - 其余（`DependencyError` 等）→ 可重试，按 60/300/900 s 退避。
    """
    task = await db.get(KbImportTask, task_id)
    if task is None:
        raise NotFoundError("导入任务不存在")

    if task.status in (KbImportTask.STATUS_SUCCESS,):
        logger.info("任务已完成，跳过：%s", task_id)
        return task.total_chunks

    if not task.document_id:
        # `supplement` 类型的任务在用户真正上传文件前就是这个样子（§9 #64）。
        # 此时没有任何输入，直接判永久失败，好过让 worker 空转三次。
        raise UnsafePayloadError("任务尚未关联知识单元（尚未上传文件）")

    doc = await db.get(KbDocument, task.document_id)
    if doc is None or doc.deleted_at is not None:
        # 文档已被删除 —— 不要再往里写切片，否则会造出「已删除文档的孤儿切片」，
        # 而它们**仍然是可被检索的**。
        raise UnsafePayloadError("知识单元已删除，任务终止")

    await _set_task_progress(db, task, KbImportTask.STATUS_PARSING, _PROGRESS_PARSED // 2)
    markdown = await _resolve_markdown(db, doc, task)
    await _set_task_progress(db, task, KbImportTask.STATUS_PARSING, _PROGRESS_PARSED)

    await _set_task_progress(db, task, KbImportTask.STATUS_EMBEDDING, _PROGRESS_CHUNKED)
    count = await _index_document(db, doc, markdown)

    doc.chunk_count = count
    await _set_task_progress(
        db,
        task,
        KbImportTask.STATUS_SUCCESS,
        _PROGRESS_DONE,
        total_chunks=count,
        finished_at=_now(),
        error_msg=None,
    )
    logger.info("导入完成：task=%s document=%s chunks=%d", task_id, doc.id, count)
    return count


async def run_rebuild_pipeline(db: AsyncSession, document_id: str) -> int:
    """正文编辑后全量重建切片与向量（PRD §6.5）。

    只从 `source_markdown` 起步 —— 不重新调 MinerU。管理员改的是 markdown，
    再解析一遍原始文件会把他刚做的修改**覆盖掉**。

    ⚠️ 本路径**不建 `kb_import_task` 行**（字典 §5.2 的任务枚举里没有
       `rebuild` 这一类，不发明枚举值）。代价是没有进度可轮询、
       失败也不留记录 —— 已登记为待确认项，见 spec §10。
    """
    doc = await db.get(KbDocument, document_id)
    if doc is None or doc.deleted_at is not None:
        raise NotFoundError("知识单元不存在")
    if not doc.source_markdown:
        raise UnsafePayloadError("该知识单元没有正文，无法重建索引")

    count = await _index_document(db, doc, doc.source_markdown)
    doc.chunk_count = count
    await db.commit()
    logger.info("索引重建完成：document_id=%s chunks=%d", document_id, count)
    return count
