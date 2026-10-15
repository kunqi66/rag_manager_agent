"""认证路由（be-01）。

四条路径**全部出自 PRD §8.1**，无一自创：公开（`login` / `refresh`）与
登录（`logout` / `me`）。字典 §5.6 的「认证」域逐字就是这两档 ——
本模块**不引入任何新的权限码**。

分层铁律：本文件只做「声明端点 → 注入依赖 → 转发 service → `ok()` 包装」，
**不写 SQL、不写哈希、不写业务判断**。

🔴 安全红线 1（PRD §11.3 R3）：身份一律来自 `Depends(get_current_user)` 解析的 JWT。
   `login` / `refresh` **只接受 `username`+`password` / `refresh_token`**，
   绝不接受任何形如 `user_id` / `roles` / `depts` 的入参。
   壳子里的 `SearchRequest.user_context` / `AskRequest.user_context`
   （见 `backend/api/query_router.py`）是同类越权隐患，由 be-03 §7.1 在 P1 出口前删除，
   **本模块不得复活这类入参**。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.deps import get_current_user, get_db, get_redis
from backend.core.response import ok
from backend.schemas.auth import (
    AuthContext,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    TokenPair,
    UserDetail,
)
from backend.service import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/login", summary="登录", response_model=None)
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    """校验账号密码，签发 access + refresh token。

    公开端点，靠防爆破 + 锁定兜底（PRD §8.1 标「公开」），**无权限码**。
    """
    # 来源 IP 仅用于日志留痕。`X-Forwarded-For` 的信任边界是待确认项
    # （be-01 §10-81 / be-03 §10-47），此处取直连对端地址，不信任转发头。
    ip = request.client.host if request.client else None
    pair: TokenPair = await auth_service.login(db, redis, payload, ip)
    return ok(pair)


@router.post("/refresh", summary="刷新令牌", response_model=None)
async def refresh(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    """用 refresh token 换新的 access token。

    公开端点，但内部会校验签名 / 过期 / **类型**与 refresh 白名单。
    """
    pair: TokenPair = await auth_service.refresh(db, redis, payload)
    return ok(pair)


@router.post("/logout", summary="登出", response_model=None)
async def logout(
    payload: LogoutRequest,
    ctx: AuthContext = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> dict:
    """吊销 refresh token 并清除会话缓存。

    只要求登录态（PRD §8.1 标「登录」），无权限码 ——
    不要「顺手加固」成需要某权限码，那会改变契约。
    """
    await auth_service.logout(redis, ctx, payload)
    return ok()


@router.get("/me", summary="当前用户信息", response_model=None)
async def me(
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """返回当前用户画像（含部门名与权限码列表）。

    同样只要求登录态、无权限码。返回的 `perms` **仅供前端隐藏 UI**，
    不构成安全边界 —— 真正拦截在 be-03 的 `require_permission`（安全红线 7）。
    """
    detail: UserDetail = await auth_service.get_me(db, ctx)
    return ok(detail)
