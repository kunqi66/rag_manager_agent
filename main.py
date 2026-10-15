"""RAG 知识库管理平台 —— FastAPI 运行入口。

启动方式（项目根目录下执行）：
    uv run main.py
    # 或
    uv run uvicorn main:app --reload --host 127.0.0.1 --port 8000

接口文档：
    http://127.0.0.1:8000/docs

容器内启动（docker compose）：
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api import (
    auth_router,
    data_mining_router,
    import_router,
    org_router,
    query_router,
    stats_router,
)
from backend.clients import milvus_client, redis_client
from backend.core.db import SchemaStateError, assert_schema_ready, engine
from backend.core.exceptions import ErrorCode, register_exception_handlers
from backend.core.logging import setup_logging
from backend.core.settings import settings
from backend.service import faq_service

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动装载 FAQ 索引，关闭时释放连接池与后台任务。

    ⚠️ 不挂这个钩子的话，`redis_client.close_client()` 与 `engine.dispose()`
    就是**定义了却没人调用**的死代码 —— 进程退出时靠 OS 回收连接，
    本地看不出来，但 `uvicorn --reload` 每次重启、以及容器滚动更新时
    都会攒下泄漏的连接，直到打满 MySQL / Redis 的 `max_connections`
    （这个故障出现时离「加了 lifespan」已经隔了很久，很难反查）。

    ⚠️ `faq_service.startup()` **自己吞掉装载异常**（见其 docstring）：
       FAQ 缓存装载失败只应降级为「缓存不命中」，不该让 API 起不来。

    🔴 **但 schema 缺失不属于「可降级」**：`assert_schema_ready()` 的失败会**直接
       让启动失败**。缺表时进程照样能起来、`/health` 照样回 up，只有请求打过来
       才 500 —— 那种「启动看起来正常」的状态比启动失败难查得多。
       修复动作是 `uv run alembic upgrade head`（容器编排里由 `migrate` 服务负责）。
    """
    try:
        # ① 先确认 schema 已就绪。**必须排在 FAQ 装载之前** —— 否则 FAQ 那一步
        #    会因为缺表而吞掉异常、留下一句「降级」日志，把真正的原因盖过去。
        await assert_schema_ready()

        # ② be-08：装载进程内 FAQ 索引 + 起「订阅刷新 / 定时重载」两个后台任务。
        #    放在 `yield` 之前 —— 先有索引再接请求，否则启动瞬间的提问全部走不到缓存。
        await faq_service.startup()
        yield
    finally:
        # 🔴 `finally` 而非 `yield` 之后直接写，且**包住整个启动段**：
        #    启动中途抛异常时，`yield` 后面的代码根本不会执行，
        #    已建立的后台任务与连接池就成为孤儿（见下方注释）。
        await faq_service.stop_background_tasks()
        await redis_client.close_client()
        # Milvus 是同步客户端，且**懒连接**：没被用过时 close 是安全的空操作。
        milvus_client.close_client()
        await engine.dispose()


app = FastAPI(
    title=settings.app_name + " API",
    description="知识导入 / 鉴权检索问答 / 数据挖掘 三类接口入口",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # 生产环境由 Nginx 同源反代，这里主要服务本地 Vite dev server。
    # 注意：allow_credentials=True 时不能用 "*"，浏览器会直接拒绝。
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

# 认证（be-01）：登录 / 刷新 / 登出 / 当前用户
app.include_router(auth_router.router)

# 组织管理（be-02）：15 个端点，权限码来自字典 §5.6
app.include_router(org_router.router)

# 三个业务路由，分别对接 engines 下的三个引擎
app.include_router(import_router.router)        # -> backend/engines/import_engine
app.include_router(query_router.router)         # -> backend/engines/search_engine
app.include_router(data_mining_router.router)   # -> backend/engines/data_mining_engine

# 统计与审计（be-06 挂 1 个审计端点；be-07 在同一 router 追加 5 个看板端点）
app.include_router(stats_router.router)


@app.get("/health", tags=["系统"], summary="健康检查")
async def health():
    """**存活**探针：进程还在、事件循环没卡死。

    🔴 它**刻意不查数据库**。把依赖探活塞进存活探针，会让「MySQL 抖一下」
    变成「容器被编排系统重启」——重启治不好远端数据库，只会把故障放大成
    重启循环，而且日志里全是「进程反复启动」的噪声、看不到真正的原因。
    依赖是否就绪看 `/ready`。
    """
    return {"code": 0, "message": "ok", "data": {"status": "up", "env": settings.app_env}}


@app.get("/ready", tags=["系统"], summary="就绪检查")
async def ready():
    """**就绪**探针：数据库可达且 schema 已到 head。

    与 `/health` 的分工是标准的两探针模型：编排系统用 `/ready` 决定
    「能不能接流量」，用 `/health` 决定「要不要重启」。

    🔴 直接复用启动时那个 `assert_schema_ready()`，**不另写一份判断** ——
    两份实现迟早会分歧，而那时会出现「启动时判定未就绪、探针却说就绪」
    这类谁都不信的结果。代价是每次探测都查一次库：探针频率低（几十秒一次），
    两条元数据查询可忽略。
    """
    try:
        await assert_schema_ready()
    except SchemaStateError as exc:
        # 503 而非 500：这是「暂时不可用、别给我流量」，不是「服务端代码出错」。
        # 报错文案由 `assert_schema_ready` 保证不含凭据（异常原文只进日志）。
        return JSONResponse(
            status_code=503,
            content={
                "code": ErrorCode.DEPENDENCY_ERROR,
                "message": str(exc),
                "data": {"status": "not_ready"},
            },
        )
    return {"code": 0, "message": "ok", "data": {"status": "ready"}}


def main():
    """本地开发启动入口。"""
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=settings.api_port,
        reload=True,
    )


if __name__ == "__main__":
    main()
