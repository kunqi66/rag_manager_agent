"""异步数据库会话工厂。

落点依据：字典 §9 待确认 40 —— 建议新增 `backend/core/db.py` 放会话工厂，
`core/deps.py` 作组装根（允许 import service）。
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.settings import settings
from backend.models import Base

logger = logging.getLogger(__name__)

#: 全项目只允许操作的库。
#: 🔴 那台 MySQL 实例有勒索攻击的历史痕迹（只允许动 `rag_kb` 一个库）。
#:    把这条写成常量并**在迁移与启动两处都校验**，是因为 `MYSQL_DB` 是环境变量：
#:    一次复制 `.env` 时的手滑就能让 `alembic upgrade head` 把整套表建到别的库上，
#:    而那条命令**不会报任何错**。
ALLOWED_DATABASE = "rag_kb"

#: 项目根目录（`backend/core/db.py` → 上溯三层）。迁移脚本与 alembic.ini 都在那里。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

engine = create_async_engine(
    settings.database_url,
    # 云上 MySQL 会掐空闲连接。不探活的话，连接池里躺着的死连接会在
    # 下一个请求上抛 "MySQL server has gone away" —— 而且往往在闲置一段
    # 时间后才出现，本地连打时完全看不出来。
    pool_pre_ping=True,
    # 与 pre_ping 双保险：超过 1 小时的连接直接丢弃重建，
    # 规避 MySQL 默认 wait_timeout=28800 之下的半死连接。
    pool_recycle=3600,
    echo=False,
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    # 提交后仍可读对象属性。否则路由层序列化响应时会触发一轮
    # 「已过期」的补查询，甚至因为会话已关闭而抛 DetachedInstanceError。
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每个请求一个会话。"""
    async with SessionLocal() as session:
        yield session


class SchemaStateError(RuntimeError):
    """schema 未就绪。

    刻意**不继承** `BizError`：它发生在**启动期**与 `/ready` 探针里，两者都不该走
    全局异常处理器（前者没有请求上下文，后者要的是 503 而不是业务码信封）。
    """


def _script_directory() -> ScriptDirectory:
    """从仓库里的 alembic.ini 读迁移脚本目录。

    🔴 路径**锚定在项目根**而不是当前工作目录：Celery 任务与容器启动的工作目录
    未必是仓库根，用相对路径会得到一个「迁移目录不存在 ⇒ heads 为空 ⇒
    永远判定为未升级」的假故障。`alembic.ini` 保持全 ASCII（Windows GBK 坑）。
    """
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


async def assert_schema_ready() -> None:
    """只读校验：目标库正确 + 迁移已到 head + 模型表齐全。

    由 `main.py` 的 `lifespan` 在装载 FAQ 之前调用，也由 `/ready` 复用。

    🔴 **本函数只读**：不建表、不 `stamp`、更不 `create_all()`。建表只有 Alembic
       一条路径（容器编排里那个一次性的 `migrate` 服务）。这里存在的理由是：
       缺表时 FastAPI 照样能**启动成功**，只有请求打过来才 500 —— 而 FAQ 装载
       还**刻意吞掉**异常（那是可降级能力），于是「进程起来了」与「系统可用」
       完全脱钩，排查时会先去怀疑业务代码。

    报错分三种，因为它们要的处置完全不同：
      ① 连不上（网络/凭据/白名单）—— 等环境恢复，改代码没用；
      ② 版本落后 —— 跑 `uv run alembic upgrade head`；
      ③ 版本已是 head 但**表缺失** —— 表被手工删过。Alembic **不会**自动重建
         已记录为「已应用」的迁移，这条必须人工确认（见函数末尾的报错文案）。
    """
    if settings.mysql_db != ALLOWED_DATABASE:
        raise SchemaStateError(
            f"拒绝操作非 {ALLOWED_DATABASE} 的库（当前 MYSQL_DB={settings.mysql_db}）"
        )

    expected_heads = set(_script_directory().get_heads())

    # 异常只记日志、不进 message：SQLAlchemy 的 DBAPI 异常文本里可能带连接串。
    try:
        async with engine.connect() as conn:
            current_heads = await conn.run_sync(
                lambda sync_conn: set(
                    MigrationContext.configure(sync_conn).get_current_heads()
                )
            )
            table_names = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
    except SQLAlchemyError as exc:
        logger.error("schema 检查失败：数据库不可达", exc_info=exc)
        raise SchemaStateError(
            f"数据库不可达（{settings.mysql_host}:{settings.mysql_port}/"
            f"{settings.mysql_db}）。请检查实例状态、安全组白名单与 .env 中的凭据。"
        ) from exc

    if current_heads != expected_heads:
        raise SchemaStateError(
            f"数据库 schema 未升级到最新（当前 {sorted(current_heads) or ['(空)']} / "
            f"期望 {sorted(expected_heads)}）。请先执行：uv run alembic upgrade head"
        )

    # 版本对得上也要真的看表：`alembic_version` 只是一行记录，
    # 手工 `DROP TABLE` 不会让它回退 —— 那种情况下版本检查永远绿。
    missing = sorted(set(Base.metadata.tables) - table_names)
    if missing:
        raise SchemaStateError(
            f"迁移版本已是 {sorted(expected_heads)}，但下列表在库中不存在：{missing}。"
            "这通常意味着表被手工删除（版本记录不会随之回退），"
            "`alembic upgrade head` 对此是 no-op，需人工确认后重建。"
        )

    logger.info(
        "schema 检查通过：版本 %s，%d 张表齐全", sorted(expected_heads), len(table_names)
    )


@asynccontextmanager
async def fresh_session() -> AsyncGenerator[AsyncSession, None]:
    """**新建一个 engine** 并给一个会话，收尾 dispose。给 Celery 任务用。

    🔴 **不能复用上面的模块级 `engine` / `SessionLocal`。**
    SQLAlchemy 的异步 engine 把连接绑在**创建它的事件循环**上。Celery 任务用
    `asyncio.run()` 起循环、跑完就关，而模块级 engine 是进程级单例 ——
    第二次任务会复用到「上个循环遗留的连接」，报
    `AttributeError: 'NoneType' object has no attribute 'send'`，
    收尾再叠一个 `RuntimeError: Event loop is closed`。
    症状是**第一个任务成功、第二个必挂**，看起来像 worker 不稳定。

    同一个坑在 `backend/scripts/seed_org.py` 首跑时踩过（CLAUDE.md 已知坑）。
    这个函数是**全项目唯一**的「任务侧拿会话」入口 —— 各任务模块各写一份时，
    形态漂移不会有任何报错，只会某天发现某个任务「跑第二次就挂」。

    代价是每个任务建一次连接池。任务本身都是分钟级的重活，这点开销可忽略，
    换来的是「任务之间零状态耦合」。
    """
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=3600,
        echo=False,
    )
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()
