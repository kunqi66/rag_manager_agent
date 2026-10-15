"""Alembic 运行环境（异步引擎版）。

连接串从 `backend.core.settings` 取，不读 alembic.ini —— 避免
「.env 改了但迁移连的是另一个库」。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from backend.core.db import ALLOWED_DATABASE
from backend.core.settings import settings

# 🔴 必须 import 整个 models 包，让所有表注册进 Base.metadata。
# 漏掉某张表 = autogenerate 认为它「不需要动」，迁移里凭空少一张表且不报错。
from backend.models import Base

#: 目标库保护。
#: 🔴 那台 MySQL 实例上**不止 `rag_kb` 一个库**，且有勒索攻击的历史痕迹 ——
#:    项目只被授权动 `rag_kb`。`MYSQL_DB` 是环境变量，一次复制 `.env` 时的手滑
#:    就能让 `alembic upgrade head` 把整套表建到隔壁库上，而**那条命令不会报任何错**
#:    （建表成功、退出码 0），事后才发现动错了库。
#:    宁可在这里硬失败：迁移是本项目唯一能改 schema 的入口，它不动，别的路径更不该动。
#:    常量从 `backend.core.db` 引，不在这里另写一份字面量 —— 两处各写一份时，
#:    改了其中一处不会有任何提示。
if settings.mysql_db != ALLOWED_DATABASE:
    raise RuntimeError(
        f"拒绝迁移：MYSQL_DB={settings.mysql_db!r}，本项目只允许操作 "
        f"{ALLOWED_DATABASE!r}。请检查 .env。"
    )

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # 迁移是一次性进程，用 NullPool 免去连接池的收尾问题
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
