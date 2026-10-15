"""Redis 客户端封装。

对外只有两样东西：**单例客户端** 与 **异常收敛器** `guard()`。
上层拿到 `get_client()` 注入进来的客户端后，用 `await guard("OP", redis.xxx(...))`
包一下，`redis.exceptions.RedisError` 就在此转成 `DependencyError`（4001 / 502）——
**第三方异常类型绝不泄漏到上层**（分层铁律）。

刻意**不**提供 `set_ex` / `get` / `del` 这类便捷包装：它们会把上层锁死在
「只能走模块单例」上，而 FastAPI 的依赖注入要求客户端是可注入、可替换的。
单例由 `get_client()` 提供，覆盖不了测试里换客户端的需求。

🔴 **Redis 不可用时一律 fail-closed，不降级放行**（be-01 §5.1-5 / be-03 §5.2）。
   拿不到 Redis 就无法防爆破、无法校验 refresh 白名单、无法读会话缓存；
   此时「跳过防爆破继续登录」或「只信 JWT 继续放行」都等于关掉安全机制。
   运维含义：**Redis 全挂 = 无人能登录**，这是刻意的安全取舍。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import TypeVar

from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.core.exceptions import DependencyError
from backend.core.settings import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

_client: Redis | None = None


def get_client() -> Redis:
    """进程内单例。连接池由 redis-py 自己维护。

    ⚠️ `decode_responses=True` —— 否则取回的是 `bytes`，上层到处 `.decode()`，
    漏掉一处就是「JSON 解析失败」这种难查的错误。
    """
    global _client
    if _client is None:
        _client = Redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
        )
    return _client


async def close_client() -> None:
    """应用关闭时释放连接池。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def guard(op: str, awaitable: Awaitable[T]) -> T:
    """执行一次 Redis 调用，把库异常转成 `DependencyError`。

    ⚠️ 调用方必须**先构造** awaitable 再传进来（`guard("GET", redis.get(k))`）。
    `redis.get(k)` 只是造协程、不会发起 IO，异常在 `await` 时才抛出，所以这样是安全的。

    日志只记操作名，**不记键名与值** —— 键名含 user_id、值是身份快照。
    """
    try:
        return await awaitable
    except RedisError as exc:
        logger.error("Redis 操作失败：%s", op)
        raise DependencyError(f"缓存服务暂不可用（{op}）") from exc
