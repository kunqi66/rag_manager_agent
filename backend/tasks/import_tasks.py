"""知识导入相关异步任务。

链路见 PRD §6.4.1：MinerU 解析 → 结构感知切片 → BGE 向量化 → 写 MySQL + Milvus。
"""

import logging

from backend.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="import.parse_and_index",
    bind=True,
    max_retries=3,
)
def parse_and_index(self, task_id: str) -> dict:
    """解析单个文档并入库。

    TODO(P1)：实现 MinerU 提交轮询 → zip 安全解压 → 切片 → 向量化 → 双写。

    重试：MinerU 调用失败多为网络抖动或限流，按 60s 起步指数退避、上限 900s。
    ⚠️ 这里**不能**写成 retry_backoff=60 / retry_backoff_max=900 —— 那两个参数
       只对 autoretry_for= 生效，对手动的 self.retry() 会被静默忽略，实测会退回
       default_retry_delay=180s。正确写法是自己算 countdown：
           countdown = min(900, 60 * 2 ** self.request.retries)
           raise self.retry(exc=e, countdown=countdown)
       另外「文档本身损坏」这类永久失败不应重试，只重试瞬时故障。
    """
    logger.info("收到解析任务 task_id=%s（尚未实现）", task_id)
    raise NotImplementedError("P1 待实现：见 PRD §6.4")


@celery_app.task(name="import.rebuild_document_index")
def rebuild_document_index(document_id: str) -> dict:
    """正文编辑后全量重建某知识单元的切片与向量。

    TODO(P1)：实现删除旧 chunk → 重新切片 → 重新向量化。
    """
    logger.info("收到索引重建任务 document_id=%s（尚未实现）", document_id)
    raise NotImplementedError("P1 待实现：见 PRD §6.5")
