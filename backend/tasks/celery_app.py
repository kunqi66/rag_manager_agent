"""Celery 应用实例。

worker 与 beat 共用这一个实例：
    uv run celery -A backend.tasks worker -l info
    uv run celery -A backend.tasks beat   -l info
"""

from celery import Celery
from celery.schedules import crontab

from backend.core.settings import settings

celery_app = Celery(
    "rag_kb", 
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    # 任务模块在 worker 启动时导入，注册表才完整。
    # 漏写这里的模块，worker 会报 "Received unregistered task" —— 任务会丢。
    include=[
        "backend.tasks.import_tasks",
        "backend.tasks.mining_tasks",
        "backend.tasks.stats_tasks",
    ],
)

celery_app.conf.update(
    timezone="Asia/Shanghai",
    enable_utc=False,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # 结果保留 1 天即可，长期留存交给 MySQL 的业务表
    result_expires=86400,
    task_acks_late=True,
    # 预取 1 个，避免长耗时的解析任务在 worker 之间分配不均
    worker_prefetch_multiplier=1,
    task_time_limit=1800,
    task_soft_time_limit=1500,
    broker_connection_retry_on_startup=True,
)

# 定时任务：PRD §6.3.1 的定期聚类挖掘与 §8.5 的看板预聚合
celery_app.conf.beat_schedule = {
    "mine-faq-clusters-daily": {
        "task": "mining.mine_faq_clusters",
        "schedule": crontab(hour=2, minute=0),
    },
    "aggregate-stats-hourly": {
        "task": "stats.aggregate_hourly",
        "schedule": crontab(minute="*/10"),
    },
}
