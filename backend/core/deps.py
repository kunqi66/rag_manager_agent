"""FastAPI 依赖装配（be-03 安全依赖）—— 应用组装根。

本文件是**组装根（composition root）**，允许它 import `service/`（be-03 §2 的
落点约定）。纯基础设施的 `security.py` 则必须保持零业务依赖，两者不要混。

提供四样东西：
- `get_db` / `get_redis`：基础设施依赖
- `get_current_user`：从 `Authorization: Bearer` 解析出**权威**身份 `AuthContext`
- `require_permission(code)`：工厂式功能权限依赖

🔴 **本模块是 PRD §11.3 R3 越权漏洞的修复落点**（安全红线 1）：
    引擎拿到的身份必须与请求体内容无关。`get_current_user` 产出的 `AuthContext`
    是此后所有请求的唯一权威身份 —— 任何形如 `user_context` 的入参都不得被读取。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from backend.clients.redis_client import get_client
from backend.core.db import get_db
from backend.core.exceptions import ForbiddenError, UnauthorizedError
from backend.core.security import decode_token
from backend.core.settings import settings
from backend.schemas.auth import AuthContext
from backend.service import session_store
from backend.service.org_service import load_user_identity

logger = logging.getLogger(__name__)

# 🔴 `auto_error=False` 是必须的。
#   默认 True 时「缺 Authorization 头」抛的是裸 `HTTPException(403)`，
#   它会绕开 BizError 处理器，落到 `exceptions.py` 的 `_http_error` 分支 ——
#   那里 403 被映射成 **code 5000**，客户端拿到「服务内部错误」而不是「未登录」，
#   前端据此无法判断该清 token 跳登录。必须自己判断并抛 UnauthorizedError(2001/401)。
_bearer = HTTPBearer(auto_error=False)

__all__ = ["get_db", "get_redis", "get_current_user", "require_permission"]


async def get_redis() -> Redis:
    """复用 `clients/redis_client` 的进程内单例。"""
    return get_client()


def _client_ip(request: Request) -> str | None:
    """取客户端 IP，只用于审计留痕。

    🔴 **只信 `X-Real-IP`，绝不解析 `X-Forwarded-For`**（字典 §9 #47）。
    两者的区别不是「哪个更准」，而是**写入语义**不同：

    - `nginx.conf` 里 `X-Real-IP` 用 `proxy_set_header ... $remote_addr` ——
      是**覆盖**写入，客户端自己带的同名头会被 nginx 冲掉，伪造不了；
    - `X-Forwarded-For` 用 `$proxy_add_x_forwarded_for` —— 是**追加**语义，
      即 `客户端原样送来的值 + ", " + 真实地址`。于是**左半段完全由客户端控制**。

    常见的「取 XFF 第一个」写法恰好取到的就是那个可伪造值，等于把审计 IP
    的写入权交给了被审计的人。要解析 XFF 除非改成从**右往左**数可信跳数，
    而本项目单层 nginx，没有那个必要。

    取不到时返回 `None`（留空），**不回填 "unknown"** —— 那会与真实 IP 一起
    出现在筛选下拉里，看起来像真有这么个来源。
    """
    if not settings.audit_trust_proxy_ip:
        # 直连部署（无 nginx）时不信任任何代理头，用 TCP 对端地址。
        return request.client.host if request.client else None
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()[:64] or None
    # 没有 X-Real-IP 说明没走 nginx（本地直连 dev server），退化为对端地址。
    return request.client.host if request.client else None


def _remaining_ttl(payload: dict) -> int:
    """会话缓存回填用的 TTL = token 剩余寿命。

    刻意**不重新设满 30 分钟** —— 否则缓存会比 token 活得更久，
    出现「token 已过期但缓存还在」的错位（be-03 §4.2.1-2）。
    """
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return 1
    return max(int(exp - time.time()), 1)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AuthContext:
    """解析 Bearer token，返回权威身份上下文。

    FastAPI 的依赖缓存保证：同一请求内多次 `Depends(get_current_user)`
    只求值一次，不会重复解码 token / 重复查库。
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError()

    payload = decode_token(credentials.credentials, expect="access")

    user_id = str(payload.get("sub") or "")
    if not user_id:
        raise UnauthorizedError()

    snapshot = await session_store.read_session(redis, user_id)

    if snapshot is None:
        # 🔴 未命中必须「从 MySQL 重算」，**不是**「回退 JWT 载荷」。
        #   这是本模块最容易被写错的一处：JWT 里的 depts/roles/perms 是**登录瞬间**的快照，
        #   Redis 会话键才是权威副本。be-02 的失效机制（组织变更 → 删会话键）
        #   只有在「未命中即重算」时才有意义。
        #   若写成 `return AuthContext(**payload)`，删缓存的结果只是「下次多读一次 JWT」，
        #   权限收回要等 access 自然过期（≤30 min）才生效 —— 安全机制退化成摆设。
        identity = await load_user_identity(db, user_id)
        snapshot = session_store.SessionSnapshot(
            department_id=identity.department_id,
            depts=identity.depts,
            roles=identity.roles,
            perms=identity.perms,
        )
        await session_store.write_session(
            redis, user_id, snapshot, _remaining_ttl(payload)
        )

    # 🔴 **显式字段构造（白名单）**，不做 `AuthContext(**payload)` 整体解包 ——
    #   否则将来往 token 载荷里加字段会静默进入上下文（be-03 §4.2.1-3）。
    #
    # `username` 取自 JWT 载荷：会话快照里刻意不存它（它只用于显示，
    # 不参与任何鉴权判定），命中与未命中两条路径都用同一个来源，行为一致。
    return AuthContext(
        user_id=user_id,
        username=str(payload.get("username") or ""),
        department_id=snapshot.department_id,
        depts=snapshot.depts,
        roles=snapshot.roles,
        perms=snapshot.perms,
        # `ip` 不进会话快照（快照按 user 缓存、跨请求复用，存 IP 会张冠李戴），
        # 每次请求现取。这也正是它必须留在 `get_current_user` 而不能下沉到
        # session_store 的原因。
        ip=_client_ip(request),
    )


def require_permission(code: str) -> Callable[..., Awaitable[AuthContext]]:
    """工厂式权限依赖：把「要什么权限」写在路由签名上，而不是埋在函数体里的 if。

    用法二选一::

        @router.post("/api/org/users", dependencies=[Depends(require_permission("org:user:create"))])
        @router.post("/api/org/users")
        async def create_user(ctx: AuthContext = Depends(require_permission("org:user:create"))): ...

    - 返回 `AuthContext` 而非 `None`，让「只挂权限、不直接用 ctx」的路由也能二选一。
    - 判定是**朴素集合成员检查**，不引入 `*` 魔法值（字典 §9 待确认 41）：
    内置管理员通过 `sys_role_permission` 显式挂全部权限行，
    魔法值会让「这个管理员到底有哪些权限」无法从数据里读出来。

    **反例（不要这么做）**：写一个按「method != GET 就要求某权限」粗判的中间件 ——
    不同写接口的权限码本就不同（`org:user:create` vs `org:role:grant`），
    这种写法必然要么过宽要么过严。
    """

    async def _checker(ctx: AuthContext = Depends(get_current_user)) -> AuthContext:
        if code not in ctx.perms:
            # 记 user_id + 权限码，**不记 token 原文**（be-03 §7.5）
            logger.warning("权限不足：user_id=%s code=%s", ctx.user_id, code)
            raise ForbiddenError(f"缺少权限：{code}")
        return ctx

    return _checker
