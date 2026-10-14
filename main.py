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

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import data_mining_router, import_router, query_router
from backend.core.exceptions import register_exception_handlers
from backend.core.logging import setup_logging
from backend.core.settings import settings

setup_logging()

app = FastAPI(
    title=settings.app_name + " API",
    description="知识导入 / 鉴权检索问答 / 数据挖掘 三类接口入口",
    version="0.1.0",
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

# 三个业务路由，分别对接 engines 下的三个引擎
app.include_router(import_router.router)        # -> backend/engines/import_engine
app.include_router(query_router.router)         # -> backend/engines/search_engine
app.include_router(data_mining_router.router)   # -> backend/engines/data_mining_engine


@app.get("/health", tags=["系统"], summary="健康检查")
async def health():
    """存活探针。"""
    return {"code": 0, "message": "ok", "data": {"status": "up", "env": settings.app_env}}


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
