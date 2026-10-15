"""会话缓存的**唯一**实现处：键名契约 + 身份快照编解码。

═══════════════════════════════════════════════════════════════
为什么单独一个文件
═══════════════════════════════════════════════════════════════
`rag:auth:session:{user_id}` 这个快照被**两个模块**共用：

- **be-01 认证**：登录/刷新时**写**，登出时**删**；
- **be-03 安全依赖**：`get_current_user` 每次都**读**，未命中时回填。

be-01 spec §10-85 明确登记了这个缺口：「该结构目前**无权威定义处**
（字典 §4 只列用途、#37 只覆盖键名、be-03 §8 只写『按约定结构』），
**改动须 be-01 / be-03 两侧同步**」。

把键名与编解码放在这里，就是让「两侧同步」变成「本来就只有一处」——
否则任何一边改了字段名，另一边会**静默**读不到值，
表现为「权限突然全空」或「部门继承失效」，且不报错。

🔴 本文件是 `rag:auth:*` 全部四个键的唯一定义处。
   别在 `deps.py` / `auth_service.py` 里再拼一次键名。

所有 Redis 调用都经 `redis_client.guard()` 收敛异常，
客户端由调用方注入（不依赖模块单例），因此测试里可整体替换。
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis

from backend.clients import redis_client

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 键名契约（be-01 §3 / be-03 §5.2 / 字典 §4）
# ═══════════════════════════════════════════════════════════════
# db=0（REDIS_DB_CACHE）。命名空间全局统一待定（字典 §9 #37），暂定 rag:auth:*。

_SESSION = "rag:auth:session:{user_id}"
_REFRESH = "rag:auth:refresh:{jti}"
_FAIL = "rag:auth:fail:{username}"
_LOCK = "rag:auth:lock:{username}"


def session_key(user_id: str) -> str:
    """身份快照。TTL = access 有效期。"""
    return _SESSION.format(user_id=user_id)


def refresh_key(jti: str) -> str:
    """refresh 白名单。存在即可用，删除即吊销。TTL = refresh 有效期。"""
    return _REFRESH.format(jti=jti)


def fail_key(username: str) -> str:
    """连续失败计数。TTL = login_lock_minutes。"""
    return _FAIL.format(username=username)


def lock_key(username: str) -> str:
    """锁定标记。存在即拒登。TTL = login_lock_minutes。"""
    return _LOCK.format(username=username)


# ═══════════════════════════════════════════════════════════════
# 身份快照结构（be-01 §5.2）
# ═══════════════════════════════════════════════════════════════


class SessionSnapshot(BaseModel):
    """`rag:auth:session:{user_id}` 的 JSON 结构。

    刻意**不含 `username`** —— 该字段从 JWT 载荷取（只用于显示，不参与鉴权判定）。
    """

    department_id: str | None = Field(None, description="所属部门（自身）")
    depts: list[str] = Field(default_factory=list, description="有效部门集合（含全部祖先）")
    roles: list[str] = Field(default_factory=list, description="角色 ID 列表")
    perms: list[str] = Field(default_factory=list, description="功能权限码列表")


async def write_session(
    redis: Redis, user_id: str, snapshot: SessionSnapshot, ttl_seconds: int
) -> None:
    """写身份快照。TTL 必须 > 0，否则 Redis 直接报错。"""
    payload = json.dumps(snapshot.model_dump(), ensure_ascii=False, separators=(",", ":"))
    await redis_client.guard(
        "SETEX", redis.set(session_key(user_id), payload, ex=max(ttl_seconds, 1))
    )


async def read_session(redis: Redis, user_id: str) -> SessionSnapshot | None:
    """读身份快照。未命中返回 `None`（由调用方决定重算）。

    结构解析失败按**未命中**处理并告警 —— 缓存里的脏数据不该让整个请求 500，
    重算一次就能自愈（这是「失效机制允许漏删、不可删错」的延伸）。
    """
    raw = await redis_client.guard("GET", redis.get(session_key(user_id)))
    if raw is None:
        return None
    try:
        return SessionSnapshot.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError):
        logger.warning("会话快照结构非法，按未命中处理：user_id=%s", user_id)
        return None


async def delete_session(redis: Redis, user_id: str) -> int:
    """删身份快照。登出与组织变更（be-02）都走这里。"""
    return int(await redis_client.guard("DEL", redis.delete(session_key(user_id))))


# ═══════════════════════════════════════════════════════════════
# refresh 白名单（实现主动吊销 —— access 无 jti，按 PRD §6.1.3 不做黑名单）
# ═══════════════════════════════════════════════════════════════


async def whitelist_refresh(redis: Redis, jti: str, user_id: str, ttl_seconds: int) -> None:
    await redis_client.guard(
        "SETEX", redis.set(refresh_key(jti), user_id, ex=max(ttl_seconds, 1))
    )


async def is_refresh_valid(redis: Redis, jti: str, user_id: str) -> bool:
    """白名单里存在**且**属于同一用户才有效。"""
    stored = await redis_client.guard("GET", redis.get(refresh_key(jti)))
    return stored is not None and stored == user_id


async def revoke_refresh(redis: Redis, jti: str) -> int:
    return int(await redis_client.guard("DEL", redis.delete(refresh_key(jti))))


# ═══════════════════════════════════════════════════════════════
# 登录防爆破（be-01 §5.1）
# ═══════════════════════════════════════════════════════════════


async def is_locked(redis: Redis, username: str) -> bool:
    return bool(await redis_client.guard("EXISTS", redis.exists(lock_key(username))))


async def register_failure(redis: Redis, username: str, lock_minutes: int) -> int:
    """失败计数 +1，返回新计数。

    首次失败时补 `EXPIRE` —— 不补的话计数永不过期，
    偶尔打错几次密码的账号会被**永久**累计到锁定阈值。
    """
    key = fail_key(username)
    count = int(await redis_client.guard("INCR", redis.incr(key)))
    if count == 1:
        await redis_client.guard("EXPIRE", redis.expire(key, lock_minutes * 60))
    return count


async def set_locked(redis: Redis, username: str, lock_minutes: int) -> None:
    await redis_client.guard(
        "SETEX", redis.set(lock_key(username), "1", ex=lock_minutes * 60)
    )


async def clear_failures(redis: Redis, username: str) -> None:
    """登录成功后清除失败计数与锁定标记。"""
    await redis_client.guard("DEL", redis.delete(fail_key(username), lock_key(username)))
