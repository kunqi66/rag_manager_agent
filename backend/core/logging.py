"""日志配置。

统一走 logging，业务代码禁止 print。
"""

import logging
import sys
from logging.config import dictConfig

from backend.core.settings import settings


def setup_logging() -> None:
    """在应用启动时调用一次，配置根 logger。"""
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": sys.stdout,
                },
            },
            "root": {
                "handlers": ["console"],
                "level": settings.log_level.upper(),
            },
            "loggers": {
                # uvicorn 自带的 handler 会重复输出，清掉交给 root
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                "uvicorn.access": {"handlers": [], "propagate": True},
                # pymilvus 的连接重试日志过于聒噪
                "pymilvus": {"level": "WARNING"},
            },
        }
    )
    logging.getLogger(__name__).info(
        "日志已初始化，环境=%s，级别=%s", settings.app_env, settings.log_level
    )
