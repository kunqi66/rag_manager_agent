"""运营看板预聚合任务。

看板只查聚合表，不实时扫 qa_log 大表。见 PRD §8.5。
"""

import logging

from backend.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="stats.aggregate_hourly")
def aggregate_hourly() -> dict:
    """把 qa_log 汇总进 stats_hourly / stats_daily。

    TODO(P2)：见 PRD §8.5。由 beat 每 10 分钟触发。
    """
    logger.info("触发看板预聚合（尚未实现）")
    raise NotImplementedError("P2 待实现：见 PRD §8.5")
