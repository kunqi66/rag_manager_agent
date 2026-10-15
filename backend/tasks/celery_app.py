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
        "backend.tasks.audit_tasks",
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
    # FAQ 聚类挖掘（be-08）。⚠️ `hour=2, minute=37` **刻意避开整点**，
    # 理由同下面两条清理任务（字典 §9 #116）：`aggregate-stats-hourly` 是
    # `*/10` 的间隔型计划，会命中所有整点与所有 10 分钟刻度。
    # 聚类是 O(N²) 的 CPU 密集任务（并查集 + 分块矩阵乘），
    # 与预聚合撞在同一分钟 = 两者都变慢，而排查时会先怀疑预聚合。
    # ⚠️ 排在 02:37（而不是 02:00）还有个额外好处：02:00 前后是各类整点任务
    # 与运维脚本的高峰，晚 37 分钟能让它拿到一段更安静的时间。
    "mine-faq-clusters-daily": {
        "task": "mining.mine_faq_clusters",
        "schedule": crontab(hour=2, minute=37),
    },
    "aggregate-stats-hourly": {
        "task": "stats.aggregate_hourly",
        "schedule": crontab(minute="*/10"),
    },
    # 审计保留期清理（be-06）。⚠️ `hour=3, minute=17` 是**刻意避开整点**的：
    # `aggregate-stats-hourly` 是 `*/10` 的间隔型计划，会命中所有整点。
    # 两个重任务撞在同一分钟，表现为「偶尔有一次预聚合跑得特别慢」——
    # 排查时会去查预聚合本身，而真凶是隔壁的 DELETE（字典 §9 #116）。
    "purge-audit-logs-daily": {
        "task": "audit.purge_audit_logs",
        "schedule": crontab(hour=3, minute=17),
    },
    # 看板小时桶清理（be-07）。`hour=4, minute=23` 同样**刻意避开整点**，
    # 理由同上一项（字典 §9 #116）—— 而且这里更要注意：
    # 本任务是 `DELETE`，整点又是 `*/10` 预聚合的必中时刻，
    # 撞上就是「预聚合变慢」，而真凶是这条 DELETE。
    # ⚠️ 排班时刻**不是**可以随手改的东西：改 `task` 键名会让这条计划失效
    # 而 `beat` **不报任何错**（找不到就跳过），症状是「表一直在涨」。
    "purge-stats-daily": {
        "task": "stats.purge_stats",
        "schedule": crontab(hour=4, minute=23),
    },
    # 知识缺口聚合（be-09）。**刻意与 `aggregate-stats-hourly` 同为 `*/10`**，
    # 即两者会在同一分钟起跑 —— 这是**故意**的，不是忘了错开：
    # 看板的「知识覆盖率」与缺口池**同源**（都数未命中 `qa_log`），
    # 同一分钟的快照才让两处数字在排查时可比（字典 §9 #143 记了两者的口径差）。
    # ⚠️ 这不违反 #116 那条「重任务别撞分钟」的教训：那条针对的是 O(N²) 聚类
    #    与大批量 DELETE；本任务只读 `qa_log` 且已有单轮扫描上限。
    "aggregate-knowledge-gaps": {
        "task": "mining.aggregate_knowledge_gaps",
        "schedule": crontab(minute="*/10"),
    },
}
