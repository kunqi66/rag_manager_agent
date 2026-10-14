"""知识沉淀挖掘相关异步任务。

链路见 PRD §6.3：日志聚类 → 候选 FAQ 生成 / 知识缺口聚合。
"""

import logging

from backend.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="mining.mine_faq_clusters")
def mine_faq_clusters(lookback_days: int | None = None) -> dict:
    """聚类历史提问，生成候选 FAQ。

    TODO(P3)：见 PRD §6.3.1。由 beat 每日 02:00 触发，
    也可通过 POST /api/mining/faq/mine 手动触发。
    """
    logger.info("触发 FAQ 聚类挖掘 lookback_days=%s（尚未实现）", lookback_days)
    raise NotImplementedError("P3 待实现：见 PRD §6.3.1")


@celery_app.task(name="mining.aggregate_knowledge_gaps")
def aggregate_knowledge_gaps() -> dict:
    """聚合未命中提问为知识缺口项。

    TODO(P3)：见 PRD §6.3.3。
    """
    logger.info("触发知识缺口聚合（尚未实现）")
    raise NotImplementedError("P3 待实现：见 PRD §6.3.3")
