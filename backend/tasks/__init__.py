"""Celery 任务包。

任务模块在此显式导入，保证 worker 启动时完成注册。
"""

from backend.tasks.celery_app import celery_app

__all__ = ["celery_app"]
