"""认证编排（be-01）。

职责：校验账号密码、签发 access + refresh、登录时**一次性**算出此后所有请求的
唯一权威身份快照、防爆破计数与锁定、refresh / logout / me。

**明确不做**（be-01 §1.2 的划界，防职责膨胀）：
- JWT 解析与密码哈希算法 —— 归 be-03（本模块只调 `create_*_token` /
  `verify_password`，**不自己 `jwt.decode`**，也不 `import hashlib`）；
- `effective_depts` / `roles` / `perms` 的计算 —— 归 be-02 `load_user_identity`；
- 功能权限判定 —— 归 be-03（`logout` / `me` 只要登录态，**无权限码**）；
- access token 黑名单 —— PRD §6.1.3 明确「依赖短有效期自然过期，不做黑名单」；
- ~~写审计日志 —— 归 be-06（P2）~~ → **be-06 已落地，本模块是它的调用点之一**（3 处：
  `auth.login` / `auth.login_failed` / `auth.logout`，见 `P2-12-be-audit` §5.4）。
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from backend.core.exceptions import UnauthorizedError
from backend.core.security import (
    FAKE_PASSWORD_HASH,
    create_access_token,
    create_refresh_token,
    decode_token,
    make_jti,
    verify_password,
)
from backend.core.settings import settings
from backend.models import SysDepartment, SysUser
from backend.models.audit_log import ANONYMOUS_USER_ID
from backend.schemas.auth import (
    AuthContext,
    DepartmentBrief,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    TokenPair,
    UserDetail,
)
from backend.service import audit_service, session_store
from backend.service.org_service import load_department_names, load_user_identity

logger = logging.getLogger(__name__)

_ACCESS_TTL_SECONDS = settings.access_token_expire_minutes * 60
_REFRESH_TTL_SECONDS = settings.refresh_token_expire_days * 60 * 60 * 24

# 登录失败统一文案。**不区分「用户不存在」与「密码错误」**（防枚举，be-01 §6）。
_INVALID_CREDENTIALS = "用户名或密码错误"


async def _audit_login_failed(
    username: str, ip: str | None, *, reason: str, user_id: str | None = None
) -> None:
    """留痕一次登录失败。

    🔴 **必须走 `record_detached`，不能用 `record()`**：失败分支紧接着就 `raise`，
    业务 session 永远不会 commit —— 用 `record()` 的话这行只是被 `add` 进 session，
    然后随 session 关闭**静默蒸发**。审计代码看起来在、库里一条都没有。

    🔴 **也绝不能去重**（不传 `dedup_seconds`）：连续 20 次失败正是最该被看见的信号，
    合并成一条等于把爆破痕迹抹平。

    `reason` 只进 `detail`，**不进响应** —— 对外仍是统一的「用户名或密码错误」
    （防账号枚举，be-01 §6）。审计页是管理员可读的，那里才该看到真实原因。
    """
    ctx = AuthContext(
        user_id=user_id or ANONYMOUS_USER_ID,
        username=username,
        ip=ip,
    )
    await audit_service.record_detached(
        ctx,
        audit_service.AuditAction.LOGIN_FAILED,
        target_type=audit_service.AuditTargetType.USER,
        target_id=user_id,
        detail={"reason": reason, "username": username},
    )


async def _register_failure(redis: Redis, username: str) -> None:
    """失败计数 +1；达阈值即置锁定标记。"""
    count = await session_store.register_failure(
        redis, username, settings.login_lock_minutes
    )
    if count >= settings.login_max_failures:
        await session_store.set_locked(redis, username, settings.login_lock_minutes)
        logger.warning("账号已锁定：username=%s 连续失败=%s", username, count)


async def login(
    db: AsyncSession, redis: Redis, payload: LoginRequest, ip: str | None
) -> TokenPair:
    """登录：校验密码 → 算身份快照 → 签发令牌。

    `ip` 仅供日志留痕。来源 IP 的取值与 `X-Forwarded-For` 的信任边界
    仍是待确认项（be-01 §10-81 / be-03 §10-47），当前由 api 层从 `Request` 取。
    """
    username = payload.username

    # ── 1. 锁定检查**先于查库** ──────────────────────────────
    # 若先查库再判锁，攻击者能靠「返回 401 还是别的」区分账号是否存在。
    if await session_store.is_locked(redis, username):
        logger.warning("登录被拒（锁定中）：username=%s ip=%s", username, ip)
        await _audit_login_failed(username, ip, reason="locked")
        raise UnauthorizedError("尝试次数过多，请稍后重试")

    # ── 2. 查用户 ────────────────────────────────────────────
    user = await db.scalar(select(SysUser).where(SysUser.username == username))

    if user is None:
        # 🔴 对**固定假哈希**跑一次完整 Argon2，让耗时与「密码错」一致。
        #   假哈希是合法的 Argon2id 串（见 core/security.py 的实测说明）——
        #   用占位字符串会让这里瞬间返回，时间差直接暴露账号是否存在。
        await run_in_threadpool(verify_password, payload.password, FAKE_PASSWORD_HASH)
        await _register_failure(redis, username)
        logger.warning("登录失败：username=%s ip=%s", username, ip)
        await _audit_login_failed(username, ip, reason="user_not_found")
        raise UnauthorizedError(_INVALID_CREDENTIALS)

    # ── 3. 校验密码（Argon2 是 CPU 密集同步调用，必须放线程池）──
    matched = await run_in_threadpool(
        verify_password, payload.password, user.password_hash
    )
    if not matched:
        await _register_failure(redis, username)
        logger.warning("登录失败：username=%s ip=%s", username, ip)
        await _audit_login_failed(username, ip, reason="bad_password", user_id=user.id)
        raise UnauthorizedError(_INVALID_CREDENTIALS)

    # ── 4. 密码通过后才看状态 ────────────────────────────────
    # 顺序刻意如此：若先判 status（用任意密码即可触发），「账号已停用」这个
    # **不同文案**就成了枚举账号存在性的入口 —— 与 §6「避免停用账号被反复试探」
    # 的目的正好相反。放在密码校验之后，试探停用账号的观感与试探普通账号完全一致。
    #
    # 且这里**不计入失败计数**（§6），密码正确的用户不会把自己锁掉。
    if user.status != SysUser.STATUS_ACTIVE:
        logger.warning("登录被拒（账号停用）：username=%s ip=%s", username, ip)
        await _audit_login_failed(
            username, ip, reason="disabled", user_id=user.id
        )
        raise UnauthorizedError("账号已停用，请联系管理员")

    # ── 5. 成功后清失败计数与锁定标记 ────────────────────────
    await session_store.clear_failures(redis, username)

    # ── 6. 算身份快照（与 be-03 缓存未命中时**同一个函数**，防漂移）──
    identity = await load_user_identity(db, user.id)
    snapshot = session_store.SessionSnapshot(
        department_id=identity.department_id,
        depts=identity.depts,
        roles=identity.roles,
        perms=identity.perms,
    )
    await session_store.write_session(redis, user.id, snapshot, _ACCESS_TTL_SECONDS)

    # ── 7. 签发令牌 ──────────────────────────────────────────
    jti = make_jti()
    refresh_token, _ = create_refresh_token(user_id=user.id, jti=jti)
    # refresh 白名单：存在即可用，登出时删除即完成主动吊销
    await session_store.whitelist_refresh(redis, jti, user.id, _REFRESH_TTL_SECONDS)

    access_token, _ = create_access_token(
        user_id=identity.user_id,
        username=identity.username,
        depts=identity.depts,  # ⚠️ effective_depts（含祖先），不是「所属部门」
        roles=identity.roles,
        perms=identity.perms,
    )

    # ── 8. 回写登录时间 ──────────────────────────────────────
    # 用 DB 的 NOW() 而非 Python 的 datetime.now()：created_at 是 server_default
    # func.now()，两处混用会因应用与数据库时区不同而出现「登录时间早于创建时间」。
    #
    # 这是本模块**唯一**的 DB 写入，与前面的读取分开、独立提交。
    # **不把 Redis 写入套进这个事务** —— Redis 无回滚，套事务只会制造
    # 「事务回滚了缓存还在」的假象。
    await db.execute(
        update(SysUser).where(SysUser.id == user.id).values(last_login_at=func.now())
    )
    # 留痕走 `record()`：与上面那笔 `last_login_at` **同一个事务**，
    # 由本函数末尾的 commit 一起落盘。此处 **不 commit**（见 audit_service 的约束）。
    audit_service.record(
        db,
        AuthContext(
            user_id=identity.user_id,
            username=identity.username,
            department_id=identity.department_id,
            depts=identity.depts,
            roles=identity.roles,
            perms=identity.perms,
            ip=ip,
        ),
        audit_service.AuditAction.LOGIN,
        target_type=audit_service.AuditTargetType.USER,
        target_id=user.id,
    )
    await db.commit()

    logger.info("登录成功：username=%s ip=%s", username, ip)
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=_ACCESS_TTL_SECONDS,
    )


async def refresh(db: AsyncSession, redis: Redis, payload: RefreshRequest) -> TokenPair:
    """刷新 access token。

    基线：**不轮换 refresh**（be-01 §10-29）—— 原样返回同一个 refresh token。
    """
    # `expect="refresh"` 必不可少：refresh 的 sub 也是合法 user_id、签名也对，
    # 不校验 typ 就会让 refresh 被当 access 用（be-03 §10-38）。
    token_payload = decode_token(payload.refresh_token, expect="refresh")

    user_id = str(token_payload.get("sub") or "")
    jti = str(token_payload.get("jti") or "")
    if not user_id or not jti:
        raise UnauthorizedError("登录凭证无效，请重新登录")

    # 白名单校验：登出 / 被吊销后这里必然落空
    if not await session_store.is_refresh_valid(redis, jti, user_id):
        raise UnauthorizedError("登录已失效，请重新登录")

    # 重算身份：内含「用户已删除 / 已停用」的校验
    identity = await load_user_identity(db, user_id)

    snapshot = session_store.SessionSnapshot(
        department_id=identity.department_id,
        depts=identity.depts,
        roles=identity.roles,
        perms=identity.perms,
    )
    # 刷新会话缓存 TTL，并借机把权限变更同步进来
    await session_store.write_session(redis, user_id, snapshot, _ACCESS_TTL_SECONDS)

    access_token, _ = create_access_token(
        user_id=identity.user_id,
        username=identity.username,
        depts=identity.depts,
        roles=identity.roles,
        perms=identity.perms,
    )

    return TokenPair(
        access_token=access_token,
        refresh_token=payload.refresh_token,
        expires_in=_ACCESS_TTL_SECONDS,
    )


async def logout(redis: Redis, ctx: AuthContext, payload: LogoutRequest) -> None:
    """登出：尽力吊销 refresh 白名单 + 清会话缓存。

    ⚠️ 登出后该 access 在**剩余有效期（≤30 min）内仍然能解析通过** ——
    access 无 `jti`，按 PRD §6.1.3 不做黑名单（窗口见 be-01 §10-28）。
    它无法再换新 access（refresh 已吊销）。
    """
    if payload.refresh_token:
        try:
            token_payload = decode_token(payload.refresh_token, expect="refresh")
            jti = str(token_payload.get("jti") or "")
            # 只允许吊销自己的 token —— 否则拿到别人的 refresh 就能把别人踢下线
            if jti and str(token_payload.get("sub") or "") == ctx.user_id:
                await session_store.revoke_refresh(redis, jti)
            else:
                logger.warning(
                    "登出时 refresh token 与当前用户不符，跳过吊销：user_id=%s",
                    ctx.user_id,
                )
        except UnauthorizedError:
            # 登出是**尽力而为**：token 已过期或被改，不能让用户「登不出去」。
            # 代价是白名单条目留到 TTL 自然过期（≤7 天），但它本就已不可用。
            logger.warning(
                "登出时 refresh token 无法解析，跳过吊销：user_id=%s", ctx.user_id
            )
    else:
        # §10-27：不带 refresh_token 则无法定位 jti，只能清会话缓存。
        # 留一条告警，便于排查「登出后仍能换新 access」的反馈。
        logger.warning(
            "登出未携带 refresh token，白名单未被吊销：user_id=%s", ctx.user_id
        )

    await session_store.delete_session(redis, ctx.user_id)

    # ⚠️ 走 `record_detached`：本函数签名里**没有 `db`**（登出只动 Redis），
    #    因此没有业务事务可搭。用 `record()` 会因为「求不到 session」而写不出来。
    await audit_service.record_detached(
        ctx,
        audit_service.AuditAction.LOGOUT,
        target_type=audit_service.AuditTargetType.USER,
        target_id=ctx.user_id,
    )


async def get_me(db: AsyncSession, ctx: AuthContext) -> UserDetail:
    """当前用户画像。

    `depts` / `roles` / `perms` **直接用 `ctx` 里的**——它本就是权威快照，
    无需再算（be-01 §5.5）。补两次查询：

    ① `sys_user` ⟕ `sys_department` —— 拿 `real_name` / `email` 与**自身**部门的
       `id`/`name`/`path`。该 JOIN 只出自身一行，**给不出祖先的名字**；
    ② `load_department_names` —— 拿整条部门链的名字，组装 `dept_names`。
    """
    row = (
        await db.execute(
            select(
                SysUser.real_name,
                SysUser.email,
                SysDepartment.id,
                SysDepartment.name,
                SysDepartment.path,
            )
            .outerjoin(SysDepartment, SysDepartment.id == SysUser.department_id)
            .where(SysUser.id == ctx.user_id)
        )
    ).first()

    if row is None:
        # be-01 spec §10-83：基线是 `NotFoundError`(3001/404)，但本实现采用该节的
        # **建议**改 2001。理由：token 有效而用户已删除，语义上是**身份失效**。
        # 回 404 会让前端提示「资源不存在」而**不清 token**，用户卡在
        # 「打不开页面又退不出去」的死循环里；401 才能触发清 token 跳登录页。
        raise UnauthorizedError("登录已失效，请重新登录")

    real_name, email, dept_id, dept_name, dept_path = row

    department = (
        DepartmentBrief(id=dept_id, name=dept_name, path=dept_path)
        if dept_id and dept_name and dept_path
        else None
    )

    # 🔴 `dept_names` 必须**遍历 `ctx.depts` 回填**，不能遍历查询结果拼接 ——
    # `WHERE id IN (...)` 的返回顺序由数据库决定，按结果顺序拼会与 `depts` 错位，
    # 表现为「名字与 id 对不上」：不报错、不抛异常，只是侧栏静默显示错的部门名。
    # 这里 dict 只当查找表用，`ctx.depts` 才是顺序的唯一来源（be-01 spec §4.1 / §10-86）。
    names = await load_department_names(db, ctx.depts)
    missing = [d for d in ctx.depts if d not in names]
    if missing:
        # 部门被删但用户仍挂在它下面（或部门链里残留了已删 id）—— 数据不一致。
        # 回退成 id 本身而不是丢弃，是为了保住 len(dept_names) == len(depts) 这条不变式；
        # 长度一旦错位，前端 `slice(-2)` 就会取到错误的部门。
        logger.warning(
            "dept_names 有回退项，sys_department 与用户有效部门集合不一致 user_id=%s missing=%s",
            ctx.user_id,
            missing,
        )
    dept_names = [names.get(d, d) for d in ctx.depts]

    return UserDetail(
        user_id=ctx.user_id,
        username=ctx.username,
        real_name=real_name,
        email=email,
        # 三个部门字段并存不可互替：`department` 仅自身（显示）、`depts` 含祖先（ACL）、
        # `dept_names` 与 `depts` 同序同长（显示）。
        department=department,
        depts=ctx.depts,
        dept_names=dept_names,
        roles=ctx.roles,
        perms=ctx.perms,
    )
